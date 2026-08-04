#!/usr/bin/env python3
"""Headless test suite for eyes. Runs against a throwaway XDG_STATE_HOME —
your real ~/.local/state/eyes is never touched. Needs `node` for the
WebCrypto-interop section (skipped if absent)."""
import base64, importlib.machinery, importlib.util, json, os, secrets, shutil, socket, ssl
import subprocess, sys, threading, time, urllib.request, urllib.error

import tempfile
STATE_ROOT = tempfile.mkdtemp(prefix="eyes-test-")
EYES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eyes")
PORT = 18443
os.environ["XDG_STATE_HOME"] = STATE_ROOT

def load_eyes():
    spec = importlib.util.spec_from_loader("eyes_mod",
        importlib.machinery.SourceFileLoader("eyes_mod", EYES))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))

# tiny valid JPEG (SOI..EOI with minimal segments), padded to look real
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffc2000b080001000101011100ffc40014"
    "0001000000000000000000000000000000000affda0008010100013f10"
) + b"\xff\xd9"

def unverified():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c

def req(url, data=None, method=None, ctype="image/jpeg", timeout=10):
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data is not None:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, context=unverified(), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def wait_port(port, secs=15):
    end = time.time() + secs
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False

def start_server(*extra):
    p = subprocess.Popen([EYES, "serve", "--port", str(PORT), "--bind", "127.0.0.1", *extra],
        env={**os.environ}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert wait_port(PORT), "server did not come up"
    return p

def main():
    eyes = load_eyes()
    S = os.path.join(STATE_ROOT, "eyes")

    # ---- 1. AES-256-GCM correctness vs published vector (McGrew-Viega TC 15)
    g = eyes.GCM()
    key = bytes.fromhex("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
    iv  = bytes.fromhex("cafebabefacedbaddecaf888")
    pt  = bytes.fromhex("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
                        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255")
    ct  = bytes.fromhex("522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
                        "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662898015ad")
    tag = bytes.fromhex("b094dac5d93471bdec1a502270e3cc6c")
    check("gcm: decrypt matches published AES-256-GCM test vector",
          g.open(key, iv, ct + tag) == pt)
    check("gcm: encrypt matches published vector", g.seal(key, iv, pt) == ct + tag)

    # ---- 2. roundtrip / wrong key / tamper
    k2 = secrets.token_bytes(32)
    iv2 = secrets.token_bytes(12)
    sealed = g.seal(k2, iv2, JPEG)
    check("gcm: roundtrip", g.open(k2, iv2, sealed) == JPEG)
    check("gcm: wrong key rejected", g.open(secrets.token_bytes(32), iv2, sealed) is None)
    tampered = bytearray(sealed); tampered[5] ^= 1
    check("gcm: tampered ciphertext rejected", g.open(k2, iv2, bytes(tampered)) is None)

    # ---- 3. WebCrypto interop: node encrypts exactly like the phone page JS
    kb64 = base64.urlsafe_b64encode(k2).decode().rstrip("=")
    node_js = f"""
    const kb64 = "{kb64}";
    let s = kb64.replace(/-/g,"+").replace(/_/g,"/"); s += "=".repeat((4-s.length%4)%4);
    const raw = Uint8Array.from(atob(s), c => c.charCodeAt(0));
    const jpeg = Buffer.from("{JPEG.hex()}", "hex");
    (async () => {{
      const ekey = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt"]);
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const ct = new Uint8Array(await crypto.subtle.encrypt({{name:"AES-GCM", iv:iv}}, ekey, jpeg));
      const out = new Uint8Array(16 + ct.length);
      out.set([69,89,69,49]); out.set(iv, 4); out.set(ct, 16);
      process.stdout.write(Buffer.from(out).toString("hex"));
    }})();
    """
    if shutil.which("node"):
        r = subprocess.run(["node", "-e", node_js], capture_output=True, text=True)
        frame = bytes.fromhex(r.stdout.strip())
        check("webcrypto: node emits EYE1 frame", frame[:4] == b"EYE1")
        check("webcrypto: python decrypts node's AES-GCM frame",
              g.open(k2, frame[4:16], frame[16:]) == JPEG)
    else:
        print("SKIP  webcrypto interop (node not installed)")

    # ---- 4. LAN server integration
    p = start_server()
    try:
        tok = open(os.path.join(S, "token")).read().strip()
        base = f"https://127.0.0.1:{PORT}/{tok}"
        st, body = req(base + "/ping")
        check("lan: ping with token", st == 200 and b'"ok"' in body)
        st, body = req(base + "/")
        check("lan: phone page served", st == 200 and b"getUserMedia" in body)
        st, _ = req(f"https://127.0.0.1:{PORT}/WRONGTOKEN000000000000/frame", data=JPEG)
        check("lan: wrong token -> 404", st == 404)
        st, _ = req(f"https://127.0.0.1:{PORT}/ping")
        check("lan: no token -> 404", st == 404)
        st, _ = req(base + "/frame", data=JPEG)
        check("lan: plain frame accepted", st == 200)
        latest = os.path.join(S, "latest.jpg")
        check("lan: latest.jpg has exact bytes", open(latest, "rb").read() == JPEG)
        hist = os.listdir(os.path.join(S, "history"))
        check("lan: history file written", len(hist) == 1)
        check("lan: latest is hardlink of history (single copy)",
              os.stat(latest).st_nlink == 2)
        st, _ = req(base + "/frame", data=b"\x00" * 100)
        check("lan: non-jpeg -> 400", st == 400)
        st, _ = req(base + "/frame", data=b"\xff\xd8" + b"\x00" * (11 * 1024 * 1024))
        check("lan: oversized (11MB) -> 413", st == 413)
        st, body = req(base + "/ca")
        check("lan: CA cert downloadable", st == 200 and b"BEGIN CERTIFICATE" in body)
        st, body = req(base + "/latest.jpg")
        check("lan: latest.jpg viewable in lan mode", st == 200 and body == JPEG)

        # eyes latest / eyes wait
        r = subprocess.run([EYES, "latest"], env=os.environ, capture_output=True, text=True)
        check("cli: eyes latest prints path", r.stdout.strip() == latest and r.returncode == 0)
        w = subprocess.Popen([EYES, "wait", "--timeout", "10"], env=os.environ,
                             stdout=subprocess.PIPE, text=True)
        time.sleep(0.6)
        req(base + "/frame", data=JPEG)
        out, _ = w.communicate(timeout=10)
        check("cli: eyes wait unblocks on new frame", w.returncode == 0 and out.strip() == latest)

        # atomicity under fire: reader must always see a complete JPEG
        stop, bad = threading.Event(), []
        def reader():
            while not stop.is_set():
                try:
                    d = open(latest, "rb").read()
                    if not (d[:2] == b"\xff\xd8" and d[-2:] == b"\xff\xd9"):
                        bad.append(len(d))
                except FileNotFoundError:
                    bad.append(-1)
        t = threading.Thread(target=reader); t.start()
        payload = JPEG + b"\x00" * 300000 + b"\xff\xd9"  # bigger frame, torn writes would show
        codes = [req(base + "/frame", data=payload)[0] for _ in range(30)]
        stop.set(); t.join()
        check("lan: atomic latest.jpg (0 torn reads under 30 rapid posts)",
              not bad, f"torn={bad[:5]}")
        check("lan: rate limit kicks in on rapid burst", 429 in codes,
              f"codes={sorted(set(codes))}")
    finally:
        p.terminate(); p.wait()

    # ---- 5. gc caps
    r = subprocess.run([EYES, "gc", "--keep", "2"], env=os.environ,
                       capture_output=True, text=True)
    hist = os.listdir(os.path.join(S, "history"))
    check("cli: eyes gc trims to --keep and prints removals",
          len(hist) == 2 and "removed" in r.stdout)

    # ---- 6. remote mode (fake tunnel meta; require_e2e on)
    json.dump({"name": "eyes", "id": "test", "credentials": "/dev/null",
               "hostname": "eyes.example.dev"}, open(os.path.join(S, "tunnel.json"), "w"))
    p = start_server("--remote")
    try:
        tok = open(os.path.join(S, "token")).read().strip()
        base = f"https://127.0.0.1:{PORT}/{tok}"
        ekey = base64.urlsafe_b64decode(open(os.path.join(S, "e2e.key")).read().strip() + "==")
        check("remote: e2e key created (32 bytes, mode 600)",
              len(ekey) == 32 and oct(os.stat(os.path.join(S, "e2e.key")).st_mode)[-3:] == "600")

        st, _ = req(base + "/frame", data=JPEG)
        check("remote: PLAINTEXT frame refused (400)", st == 400)

        iv = secrets.token_bytes(12)
        good = b"EYE1" + iv + g.seal(ekey, iv, JPEG)
        st, _ = req(base + "/frame", data=good, ctype="application/octet-stream")
        check("remote: encrypted frame accepted", st == 200)
        check("remote: decrypted plaintext JPEG on disk",
              open(os.path.join(S, "latest.jpg"), "rb").read() == JPEG)

        iv = secrets.token_bytes(12)
        bad_frame = b"EYE1" + iv + g.seal(secrets.token_bytes(32), iv, JPEG)
        n_hist = len(os.listdir(os.path.join(S, "history")))
        st, body = req(base + "/frame", data=bad_frame, ctype="application/octet-stream")
        check("remote: wrong-key frame dropped (400, nothing written)",
              st == 400 and b"decrypt failed" in body
              and len(os.listdir(os.path.join(S, "history"))) == n_hist)

        st, body = req(base + "/latest.jpg")
        check("remote: GET latest.jpg disabled (403) so plaintext never transits edge",
              st == 403)
        server_log = p.stdout  # check drop was logged
    finally:
        p.terminate()
        out, _ = p.communicate()
        check("remote: decrypt failure logged", "DROPPED" in out and "decryption failed" in out)
        check("remote: plaintext refusal logged", "REFUSED plaintext" in out)
        check("remote: URL printed with #k= fragment", "#k=" in out)
        check("remote: warns tunnel not running", "tunnel is not running" in out)

    # key never in path check: fragment only
    check("remote: e2e key appears only after '#' in printed URL",
          all(("#k=" in line) == ("k=" in line.split("#")[0] + "#k=" and "#k=" in line)
              for line in out.splitlines() if "#k=" in line))

    # ---- 7. mcp server on stdio
    mcp = subprocess.Popen([EYES, "mcp"], env=os.environ, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def rpc(msg):
        mcp.stdin.write(json.dumps(msg) + "\n"); mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())
    try:
        r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params":
                 {"protocolVersion": "2025-06-18", "capabilities": {},
                  "clientInfo": {"name": "test", "version": "0"}}})
        check("mcp: initialize handshake",
              r["result"]["serverInfo"]["name"] == "eyes"
              and r["result"]["protocolVersion"] == "2025-06-18")
        mcp.stdin.write(json.dumps({"jsonrpc": "2.0",
            "method": "notifications/initialized"}) + "\n"); mcp.stdin.flush()
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check("mcp: tools/list has eyes_latest + eyes_wait",
              [t["name"] for t in r["result"]["tools"]] == ["eyes_latest", "eyes_wait"])
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "eyes_latest", "arguments": {}}})
        img = next(c for c in r["result"]["content"] if c["type"] == "image")
        raw = base64.b64decode(img["data"])
        check("mcp: eyes_latest returns the frame as base64 image",
              not r["result"]["isError"] and img["mimeType"] == "image/jpeg"
              and raw[:2] == b"\xff\xd8" and raw[-2:] == b"\xff\xd9")
        r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "eyes_wait", "arguments": {"timeout_secs": 1}}})
        check("mcp: eyes_wait times out as tool error (not a crash)",
              r["result"]["isError"] and "timed out" in r["result"]["content"][0]["text"])
        r = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "nope", "arguments": {}}})
        check("mcp: unknown tool -> JSON-RPC error", r["error"]["code"] == -32602)
        r = rpc({"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
        check("mcp: unknown method -> -32601", r["error"]["code"] == -32601)
    finally:
        mcp.stdin.close(); mcp.wait(timeout=5)
    check("mcp: clean exit on stdin close", mcp.returncode == 0)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
