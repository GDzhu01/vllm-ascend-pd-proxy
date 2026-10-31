# vLLM Ascend PD Proxy

用于 vLLM-Ascend Prefill/Decode 分离部署的独立 HTTP 负载均衡代理，新增 Anthropic `POST /v1/messages` 支持。

请求路径：客户端 → proxy → P 完成 prefill → proxy 将 P 返回的 KV 传输参数附入原请求 → D 生成回答 → 客户端。
KV 数据由后端 connector 传输，proxy 负责 HTTP 调度与参数传递。

## 1. 前提与安装

- Python 3.10 或更新版本；`proxy.sh` 在 Bash 环境运行。
- 已经启动并打通 KV connector 的 P、D 服务；本仓库不会启动模型服务。
- 使用 `/v1/messages` 时，P、D 都必须提供该接口；P 必须接受并返回 vLLM 的 `kv_transfer_params` 扩展。
- proxy 本身无需安装 vLLM、torch 或 CANN。后端是否支持 Messages PD，需要按实际 vLLM 版本核对。

```bash
git clone https://github.com/GDzhu01/vllm-ascend-pd-proxy.git
cd vllm-ascend-pd-proxy
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 2. 启动 proxy

下面按 P、D 各 4 个实例举例；请改成实际地址。P、D 的 HTTP 端口都从 7100 开始递增。

```bash
P_IP=192.168.101.161 P_N=4 P_PORT0=7100 \
D_IP=192.168.101.162 D_N=4 D_PORT0=7100 \
PROXY_PORT=8081 bash proxy.sh
```

该命令使用 P 的 `7100–7103`、D 的 `7100–7103`，对外监听 `0.0.0.0:8081`。
默认单 worker，可通过 `WORKERS=2` 设置多进程；worker 共享调度状态。

后端启用 Bearer 鉴权时，在启动前设置 `OPENAI_API_KEY`，proxy 会将它用于后端的 `Authorization` 请求头。
启动脚本会清除 HTTP 代理环境变量，避免内网后端流量被转发到外部代理。

也可以直接传参数，适合各实例使用不同 IP 的部署：

```bash
python examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py \
  --host 0.0.0.0 --port 8081 \
  --prefiller-hosts 192.168.101.161 192.168.101.163 \
  --prefiller-ports 7100 7100 \
  --decoder-hosts 192.168.101.162 192.168.101.164 \
  --decoder-ports 7100 7100
```

## 3. 调用 Messages

将 `your-served-model` 替换为后端实际提供的模型名。客户端只调用 proxy，无需手工拼装 KV 参数。

```bash
curl -N --noproxy '*' http://127.0.0.1:8081/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "your-served-model",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "你好，介绍一下自己"}]
  }'
```

- `stream: true` 返回 Anthropic SSE；改为 `false` 返回 JSON。
- `system`、`tools` 和消息内容沿用后端支持的 Anthropic 格式。
- 客户端提供的 `x-api-key`、`anthropic-version`、`anthropic-beta` 会转发到 P 和 D；`X-Request-Id` 沿用已有请求 ID 路径。
- OpenAI 的 `/v1/chat/completions` 和 `/v1/completions` 继续可用。

## 4. 检查与行为边界

```bash
curl --noproxy '*' http://127.0.0.1:8081/healthcheck
curl --noproxy '*' http://127.0.0.1:8081/v1/models
```

`/healthcheck` 展示调度器状态和注册实例数量，不能据此证明后端健康或 KV 传输成功。

Messages 响应按字节透传，保留文本、工具、thinking、usage 和 SSE 事件；后端必须先支持这些能力。
Messages 路径不执行 OpenAI `choices` 解析、cached-token 改写或 OpenAI 专用 recompute 续写。
P 未返回 KV 参数时，proxy 返回 **502**，不会自动绕过 P 让 D 本地 prefill。
P 或 D 的初始 HTTP 错误保留状态码和响应体；已开始的流遇到传输错误会中断，不会自动重放。
客户端断开会关闭 Messages 上游响应并释放调度负载。

proxy 沿用示例的信任边界，没有新增客户端鉴权。部署时应通过网络访问控制或已有网关限制客户端访问。

## 5. 验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ut/proxy -q
```

本次本地验证：27 项测试通过（含原有 HTTP/调度回归，以及新增 Messages JSON、SSE 分片、请求头、错误和断开清理测试）。
未做真实 NPU 或线上模型的端到端联调，因此不据此声明实际 KV 传输或模型推理已验收。

## 来源与许可

代理及既有测试提取自 [vllm-project/vllm-ascend](https://github.com/vllm-project/vllm-ascend)，
来源提交 `27dea4cd5bdcff6e56e0fc617d0049a21bd01b18`，并包含本次 Messages 适配。
原代理源自 vLLM 的 toy proxy。此仓库是个人维护的独立提取版本，采用 [Apache-2.0](LICENSE) 许可。
更详细的接口说明见 [messages.md](examples/disaggregated_prefill_v1/messages.md)。
