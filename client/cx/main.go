// cx is a tiny "thin client" for the Python `chromectl` CLI.
//
// The plain CLI pays ~100ms of Python interpreter+import boot on every
// invocation. A resident `chromectl daemon` process pays that cost once and
// keeps warm CDP connections around; it speaks a trivial newline-delimited
// JSON protocol over a Unix socket at ~/.chromectl/daemon.sock. cx (which boots
// in ~3ms as a static Go binary) forwards the user's argv to that daemon and
// prints back the captured output.
//
// Protocol (see chromectl/daemon.py — _send/_recv/route/_process):
//
//	ping:    send {"op":"ping"}\n            -> {"ok":true}
//	command: send {"argv":[...]}\n           -> one response line, either
//	           {"ok":bool,"code":int,"stdout":str,"stderr":str}  (print + exit code)
//	           {"ok":true,"passthrough":true}                    (run locally instead)
//
// One request object per line in, one response object per line out. JSON escapes
// real newlines, so within a single response the only literal '\n' is the frame
// terminator — we read bytes until the first '\n'.
//
// This binary installs as `chromectl` (the front-facing command). Fallback:
// whenever the daemon is unreachable, replies passthrough, or the exchange
// errors, it execs the Python CLI `chromectl-py` on PATH with the same args,
// inheriting stdio and propagating its exit code. The distinct fallback name
// means it can never exec itself.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

// connectTimeout bounds establishing the connection (and the ping handshake).
// The command response read itself is left open-ended — a command may legitimately
// take a long time (page loads, waits, screenshots).
const connectTimeout = 2 * time.Second

// socketPath returns ~/.chromectl/daemon.sock. HOME is used first (matching the
// Python side's os.path.expanduser), falling back to os.UserHomeDir() so this
// also resolves on Windows where HOME is typically unset.
func socketPath() (string, error) {
	home := os.Getenv("HOME")
	if home == "" {
		var err error
		home, err = os.UserHomeDir()
		if err != nil {
			return "", err
		}
	}
	return filepath.Join(home, ".chromectl", "daemon.sock"), nil
}

// daemonResponse models the command reply. Fields absent from a given reply stay
// at their zero value (e.g. Passthrough=false for a normal command result).
type daemonResponse struct {
	OK          bool   `json:"ok"`
	Code        int    `json:"code"`
	Stdout      string `json:"stdout"`
	Stderr      string `json:"stderr"`
	Passthrough bool   `json:"passthrough"`
}

func main() {
	argv := os.Args[1:]

	// Try the daemon. If it handles the command, tryDaemon exits the process.
	// It returns (without exiting) only when we must fall back to local exec.
	tryDaemon(argv)

	// Fallback: exec the real `chromectl` on PATH with the same args.
	runLocal(argv)
}

// tryDaemon attempts to service the command via the daemon. On success it writes
// the daemon's output and calls os.Exit with the command's code — it does not
// return. It returns normally only to signal "fall back to local exec": the
// socket is missing/unreachable, the daemon replied passthrough, or a
// protocol/read error occurred.
func tryDaemon(argv []string) {
	sock, err := socketPath()
	if err != nil {
		return // cannot resolve socket path -> fall back
	}

	// A missing socket file means no daemon; skip straight to fallback.
	if _, err := os.Stat(sock); err != nil {
		return
	}

	// Connect with a short timeout so a dead/hung socket doesn't stall boot.
	conn, err := net.DialTimeout("unix", sock, connectTimeout)
	if err != nil {
		return // connection failed -> fall back
	}
	defer conn.Close()

	// One buffered reader for the whole connection, so any bytes it reads past
	// the ping line stay buffered for the command read on the same reader.
	r := bufio.NewReader(conn)

	// Ping first to confirm a live daemon actually answers (a stale socket file
	// would connect but never reply). Bound the handshake with the deadline.
	_ = conn.SetDeadline(time.Now().Add(connectTimeout))
	if !ping(conn, r) {
		return // no valid ping -> fall back
	}

	// Ping succeeded; the command itself may run long, so clear the deadline.
	_ = conn.SetDeadline(time.Time{})

	// Send the command request: one JSON object, newline-terminated.
	if err := writeJSONLine(conn, map[string]any{"argv": argv}); err != nil {
		return // write error -> fall back
	}

	// Read exactly one response line (bytes up to the first '\n').
	line, err := readLine(r)
	if err != nil {
		return // read error -> fall back
	}

	var resp daemonResponse
	if err := json.Unmarshal(line, &resp); err != nil {
		return // malformed response -> fall back
	}

	// A passthrough reply means this command must run locally.
	if resp.Passthrough {
		return
	}

	// Normal result: relay output to our own streams and exit with its code.
	fmt.Fprint(os.Stdout, resp.Stdout)
	fmt.Fprint(os.Stderr, resp.Stderr)
	os.Exit(resp.Code)
}

// ping sends {"op":"ping"} and reports whether the reply is {"ok":true}.
func ping(conn net.Conn, r *bufio.Reader) bool {
	if err := writeJSONLine(conn, map[string]any{"op": "ping"}); err != nil {
		return false
	}
	line, err := readLine(r)
	if err != nil {
		return false
	}
	var pong struct {
		OK bool `json:"ok"`
	}
	if err := json.Unmarshal(line, &pong); err != nil {
		return false
	}
	return pong.OK
}

// writeJSONLine marshals obj and writes it followed by a single '\n' terminator.
func writeJSONLine(conn net.Conn, obj any) error {
	b, err := json.Marshal(obj)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	_, err = conn.Write(b)
	return err
}

// readLine reads bytes from r up to and including the first '\n', returning the
// line without the trailing newline. Because JSON escapes real newlines, the
// only literal '\n' in a response is the frame terminator.
func readLine(r *bufio.Reader) ([]byte, error) {
	line, err := r.ReadBytes('\n')
	if err != nil && len(line) == 0 {
		return nil, err
	}
	return bytes.TrimRight(line, "\n"), nil
}

// runLocal execs the Python CLI `chromectl-py` on PATH with the same args,
// inheriting stdio, and exits with the child's exit code. The fallback target is
// deliberately a DISTINCT name from this binary (which installs as `chromectl`),
// so there is no risk of exec'ing ourselves — no PATH-order or recursion games.
// Uses os/exec (not syscall.Exec) so the same code path builds and works on Windows.
func runLocal(argv []string) {
	path, err := exec.LookPath("chromectl-py")
	if err != nil {
		fmt.Fprintln(os.Stderr, "chromectl: chromectl-py not found on PATH "+
			"(is the chromectl Python package installed?)")
		os.Exit(1)
	}

	cmd := exec.Command(path, argv...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		// Propagate the child's exit code when it exited non-zero.
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			os.Exit(exitErr.ExitCode())
		}
		// Failed to start (or was signalled without a clean code).
		fmt.Fprintf(os.Stderr, "cx: %v\n", err)
		os.Exit(1)
	}
	os.Exit(0)
}
