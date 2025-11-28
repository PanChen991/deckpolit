
# DeckPilot MVP（Open WebUI + 后端编排 + Skywork MCP SSE）

这是一个可直接运行的最小项目，用于在本地用 **Open WebUI** 调试自研后端，后端通过 **SSE** 对接 **Skywork MCP Server**，实现“生成 PPT / Doc / Excel → 返回下载链接”。

## 🔐 安全提醒
- **不要**把 Secret 写入前端或提交到仓库。
- 本项目通过 `.env` 注入 `SKYWORK_SECRET_ID/KEY` 到 **后端容器**，仅由后端与 Skywork 通信。
- MCP 指定 `sign = md5(secret_id:secret_key)`，由于签名位于 URL query，请**务必只在服务器端使用**，不要暴露给浏览器。

## 🚀 快速开始（Docker Compose）
1. 安装 Docker Desktop（Windows 需启用 WSL2）。
2. 复制环境变量：
   ```bash
   cp .env.example .env
   # 使用你在 Skywork 平台重新生成的、未泄露的密钥
   # 编辑 .env 写入 SKYWORK_SECRET_ID / SKYWORK_SECRET_KEY
   ```
3. 启动：
   ```bash
   docker compose up -d --build
   ```
4. 打开 Open WebUI：<http://localhost:3000>
   - 该 Compose 已将 `OPENAI_API_BASE_URL` 预设为 `http://backend:8000/v1`；
   - 你也可以在 WebUI → Settings → Models 中手动修改。
5. 健康检查：
   - 后端健康检查：<http://localhost:8000/health>
   - Open WebUI 新建对话，随便输入一句话，若返回“DeckPilot 已连通后端…”，说明连通成功。

## 🧪 生成 PPT（两种方式）
**方式 A：直接调用后端 REST**
```bash
curl -X POST http://localhost:8000/make-deck \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "2026 年轻型两轮电动车行业周报",
    "template_hint": "使用Tech-Blue模板，统一标题 36px，正文字号 20px，右下角放置公司Logo",
    "use_network": true,
    "mode": "ppt"
  }'
```
返回示例：
```json
{"download_url":"https://.../file.pptx"}
```

**方式 B：在 Open WebUI 里做按钮/工具**
- 你可以在 Open WebUI 里新增自定义工具（Tool/Function），采集“模板选择/主题/Logo/数据源”等参数，
  最终调用本后端 `/make-deck`。

## 🧩 接口说明
- `POST /make-deck`
  - Request JSON
    - `topic` (string) 必填
    - `template_hint` (string) 可选，将模板/品牌诉求写入自然语言
    - `use_network` (bool) 传给 MCP 的 `use_network`
    - `mode` in `["ppt","ppt-fast","doc","excel"]`
    - `outline_md` (string) 可选，直接给出 Markdown 大纲
  - Response JSON
    - `download_url` (string) 生成文件下载链接
- `POST /v1/chat/completions`：OpenAI 兼容最小回包，仅用于 Open WebUI 连通性测试。

## ⚙️ 实现细节
- 后端使用 **SSE** 与 Skywork MCP 通信（`/open/sse?secret_id=...&sign=md5(...)`）。
- 流式读取事件，直到拿到 `file_url/download_url` 字段后返回。
- 若你的实际事件格式不同，请查看官方文档并在 `consume_sse_and_get_file_url` 中调整解析。

## 🛡️ 加固建议
- 在你的后端再包一层“短时票据”（避免把目标 SSE URL 直接暴露给任何前端）。
- 增加 SSE 断线重连、超时（180s）、幂等重试与速率限制。
- 生成后的文件建议复制到你自己的对象存储（S3/OSS），确保链接长期可用。
- 若模板一致性要求高，建议后处理（如 `python-pptx`）进行主题/母版统一。

---

© DeckPilot MVP
