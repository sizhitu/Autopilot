import { Container, getContainer } from "@cloudflare/containers";

// 容器类：对应 wrangler.toml 中 class_name = "Autopilot"
export class Autopilot extends Container {
  // 容器内 uvicorn 监听的端口（Dockerfile 中 ${PORT:-8000}）
  defaultPort = 8000;
  // 无请求 10 分钟后休眠，下次访问自动唤醒（冷启动通常数秒）
  sleepAfter = "10m";
}

export default {
  async fetch(request, env) {
    // 固定 session id → 所有请求路由到同一个容器实例
    // （适合个人低频工具；如需水平扩展可改为按用户/IP 生成 session id）
    const container = getContainer(env.AUTOPILOT, "autopilot-main");
    return container.fetch(request);
  },
};
