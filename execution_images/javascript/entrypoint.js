// Reads "CF1 <src_len> <stdin_len>\n" + source + stdin from stdin, writes them to the tmpfs,
// then runs node on the source with stdin redirected from the file. No shell involved.
const fs = require("fs");
const { spawnSync } = require("child_process");

function readAll() {
  const chunks = [];
  const buf = Buffer.alloc(65536);
  for (;;) {
    let n;
    try {
      n = fs.readSync(0, buf, 0, buf.length, null);
    } catch (e) {
      if (e.code === "EAGAIN") continue;
      if (e.code === "EOF") break;
      throw e;
    }
    if (n === 0) break;
    chunks.push(Buffer.from(buf.subarray(0, n)));
  }
  return Buffer.concat(chunks);
}

const data = readAll();
const nl = data.indexOf(0x0a);
const parts = nl < 0 ? [] : data.subarray(0, nl).toString("latin1").split(" ");
if (parts.length !== 3 || parts[0] !== "CF1") {
  process.stderr.write("codeforge: bad payload\n");
  process.exit(70);
}
const n = parseInt(parts[1], 10);
const m = parseInt(parts[2], 10);
const rest = data.subarray(nl + 1);
if (rest.length !== n + m) {
  process.stderr.write("codeforge: bad payload length\n");
  process.exit(70);
}
fs.writeFileSync("/sandbox/main.js", rest.subarray(0, n));
fs.writeFileSync("/sandbox/stdin.txt", rest.subarray(n));
const fd = fs.openSync("/sandbox/stdin.txt", "r");
const r = spawnSync(process.execPath, ["/sandbox/main.js"], { stdio: [fd, "inherit", "inherit"] });
if (r.error) {
  process.stderr.write("codeforge: spawn failed\n");
  process.exit(70);
}
if (r.signal) process.kill(process.pid, r.signal);
process.exit(r.status === null ? 1 : r.status);
