import http from "node:http";

const port = Number.parseInt(process.env.COPPERMIND_SYNC_PORT || "8092", 10);
const request = http.get({ host: "127.0.0.1", port, path: "/livez", timeout: 2_000 }, (response) => {
  response.resume();
  process.exit(response.statusCode === 200 ? 0 : 1);
});
request.on("error", () => process.exit(1));
request.on("timeout", () => {
  request.destroy();
  process.exit(1);
});
