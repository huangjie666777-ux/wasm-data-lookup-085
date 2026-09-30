# Wasm Execution Sandbox

纯后端 HTTP 服务：接收第三方 WebAssembly 数据转换程序（WASI preview1），
在严格隔离的资源预算内运行其 `_start`，返回标准输出、标准错误、退出码或
明确的失败原因。没有前端，也没有模块仓库。

## 运行环境

- Python 3.10，依赖见 `requirements.txt`（FastAPI 0.115.12 / wasmtime 49.0.0）。
- 使用项目自带虚拟环境：

```bash
.venv/bin/python -m pip install -r requirements.txt   # 离线可用 --no-index --find-links .wheels
```

## 启动

```bash
.venv/bin/python -m uvicorn sandbox_service.app:app --host 127.0.0.1 --port 8081
```

健康检查与当前服务端上限：

```bash
curl -s http://127.0.0.1:8081/health
curl -s http://127.0.0.1:8081/limits
```

## HTTP 协议

### `POST /execute`

请求体为 JSON。二进制字段（`module`、`stdin`）一律使用 **标准 Base64** 文本。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `module` | string，必填 | Base64 编码的 wasm 二进制（WASI preview1 命令模块） |
| `stdin` | string，可选 | Base64 编码的标准输入，默认空 |
| `argv` | string[]，可选 | 程序参数；服务端固定以 `program` 作为 argv[0] |
| `budget` | object，可选 | 收紧资源预算，字段均可缺省（取服务端默认值） |

`budget` 字段（只能小于等于 `GET /limits` 公布的服务端最大值）：

| 字段 | 默认 | 最大值 | 含义 |
| --- | --- | --- | --- |
| `fuel` | 10,000,000,000 | 10,000,000,000 | 指令燃料，耗尽即 trap |
| `timeout_ms` | 2000 | 5000 | 墙钟执行时间（毫秒），超时从另一线程真实终止 |
| `memory_bytes` | 16 MiB | 64 MiB | 线性内存上限 |
| `output_bytes` | 1 MiB | 4 MiB | stdout+stderr 合并总字节上限 |

其他固定上限：模块 16 MiB、stdin 4 MiB、活动执行数 8。
所有上限集中在 `sandbox_service/config.py`，可用 `SANDBOX_*` 环境变量覆盖。

成功受理后统一返回 HTTP 200，执行结果通过 `status` 区分：

| `status` | 含义 | 关键字段 |
| --- | --- | --- |
| `exited` | 正常结束（含 `proc_exit` 非零退出） | `exit_code` |
| `trapped` | 程序自身陷阱（unreachable、越界、除零等） | `reason` |
| `resource_exhausted` | 燃料/墙钟/内存/输出超限 | `reason`、`stdout_truncated` |

输入问题返回 HTTP 400（`status: input_error`）；活动执行数已满时立即返回
HTTP 503（不排队等待）。响应中的 `stdout`/`stderr` 同样是 Base64；超限时
保留限额内前缀并置 `stdout_truncated: true`。

### curl 示例

先构建示例模块（使用 wasmtime 自带的 WAT 编译器，无需 wabt/编译器）：

```bash
.venv/bin/python examples/build_examples.py

MODULE=$(base64 -w0 examples/uppercase.wasm)
INPUT=$(printf 'Hello, WebAssembly!' | base64 -w0)
curl -s -X POST http://127.0.0.1:8081/execute \
  -H 'Content-Type: application/json' \
  -d "{\"module\":\"$MODULE\",\"stdin\":\"$INPUT\"}"
```

返回（节选）：

```json
{
  "status": "exited",
  "exit_code": 0,
  "stdout": "SEVMTE8sIFdFQkFTU0VNQkxZIQ==",
  "stderr": "",
  "stdout_truncated": false,
  "reason": "",
  "fuel_consumed": 584
}
```

超限示例：

```bash
M=$(base64 -w0 examples/busy_loop.wasm)
curl -s -X POST http://127.0.0.1:8081/execute -H 'Content-Type: application/json' \
  -d "{\"module\":\"$M\",\"budget\":{\"timeout_ms\":300}}"
# -> status "resource_exhausted", reason "wall-clock timeout exceeded"

M=$(base64 -w0 examples/spam_output.wasm)
curl -s -X POST http://127.0.0.1:8081/execute -H 'Content-Type: application/json' \
  -d "{\"module\":\"$M\",\"budget\":{\"output_bytes\":32}}"
# -> status "resource_exhausted", stdout_truncated true, 保留前 32 字节
```

## 隔离与安全模型

- 每次请求使用全新的 Wasmtime `Engine`/`Store`/实例，互相不共享内存。
- 只连接标准流：stdin 来自请求体（内存），stdout/stderr 捕获到内存。
- 不调用 `inherit_env`/`inherit_stdin`/`inherit_stdout`，不 `preopen_dir`，
  环境变量显式置空，不预开放任何宿主目录或网络能力。
- 仅允许导入 `wasi_snapshot_preview1` 中固定白名单函数；导入其他模块、
  非函数导入、缺少 `_start` 或非法 wasm 在执行前即被拒绝（400）。
- 资源限制在执行前生效：fuel（指令数）、线性内存上限、墙钟超时、合并输出
  字节数。墙钟与取消通过独立看门狗线程推进 engine epoch 触发真实 trap，
  死循环、无限 `memory.grow`、持续输出都会被终止。
- 并发数有硬上限；满载立即返回 503，无线程池之外的等待队列。客户端断连
  或服务关闭会取消并中断对应执行，槽位在 worker 的 `finally` 中恰好释放一次。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `sandbox_service/config.py` | 集中配置的默认值/最大值/并发度 |
| `sandbox_service/models.py` | HTTP 协议模型、Base64 与预算收紧校验 |
| `sandbox_service/validation.py` | 模块编译与导入/导出静态校验 |
| `sandbox_service/streams.py` | 有界 stdout/stderr 捕获与截断标记 |
| `sandbox_service/wasi_instance.py` | 单请求 WASI 实例、stream host calls、`_start` 执行 |
| `sandbox_service/supervisor.py` | 并发名额、线程池、墙钟看门狗、取消/关闭 |
| `sandbox_service/app.py` | FastAPI 路由与状态分类 |
| `examples/` | uppercase、busy_loop、memory_grower、spam_output、exits_nonzero |
| `tests/` | HTTP、限制、并发与隔离自测 |

## 自测

```bash
.venv/bin/python examples/build_examples.py   # 生成 examples/*.wasm
.venv/bin/python -m pytest -q
```
