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
| `sources` | string[]，可选 | 本次执行允许查询的**服务端命名资料源 ID**；缺省表示完全无网络 |
| `budget` | object，可选 | 收紧资源预算，字段均可缺省（取服务端默认值） |

`budget` 字段（只能小于等于 `GET /limits` 公布的服务端最大值）：

| 字段 | 默认 | 最大值 | 含义 |
| --- | --- | --- | --- |
| `fuel` | 10,000,000,000 | 10,000,000,000 | 指令燃料，耗尽即 trap |
| `timeout_ms` | 2000 | 5000 | 墙钟执行时间（毫秒），超时从另一线程真实终止 |
| `memory_bytes` | 16 MiB | 64 MiB | 线性内存上限 |
| `output_bytes` | 1 MiB | 4 MiB | stdout+stderr 合并总字节上限 |

查询预算（只有请求显式列出 `sources` 时才可能产生出站请求）：

| 字段 | 默认 | 最大值 | 含义 |
| --- | --- | --- | --- |
| `query_count` | 4 | 16 | 本次执行实际出站 GET 次数 |
| `query_response_bytes` | 64 KiB | 1 MiB | 单次响应正文上限，超限即失败，绝不截断成功 |
| `query_total_bytes` | 256 KiB | 4 MiB | 本次执行累计响应正文上限 |

其他固定上限：模块 16 MiB、stdin 4 MiB、活动执行数 8、查询业务键 4096 字节。
所有上限集中在 `sandbox_service/config.py`，可用 `SANDBOX_*` 环境变量覆盖。

### 受控资料源（默认无网络）

资料源在**服务端**通过环境变量 `SANDBOX_SOURCES` 命名配置，地址与认证头
只存在于宿主：

```json
{
  "device_specs": {
    "url": "http://127.0.0.1:8091/specs",
    "param": "model",
    "auth_header_name": "Authorization",
    "auth_header_value": "Bearer demo-token-123"
  }
}
```

`examples/sources.env` 是可直接 `source` 的示例。未配置或请求未列出源 ID
时执行行为与旧版一致：宿主不发起任何网络请求。请求里的未知/未授权源 ID
在执行前返回 400，同样不产生任何流量。执行请求**只能传源 ID**，不能提供
URL、路径、请求方法或凭据。`GET /limits` 的 `available_sources` 仅公布 ID。

#### 宿主调用 ABI（导入模块 `sandbox_queries`）

模块在静态导入校验时必须使用如下精确签名，否则 400：

| 函数 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `query_fetch` | `(i32 src_ptr, i32 src_len, i32 key_ptr, i32 key_len, i32 buf_ptr, i32 buf_len)` | `i64` | 发起一次受控 GET |
| `query_read` | `(i32 token, i32 offset, i32 buf_ptr, i32 buf_len)` | `i64` | 分块读取已缓存正文 |

i64 返回值打包为 `(u32 status << 32) | u32 value)`：

| status | 含义 | value |
| --- | --- | --- |
| 0 | 成功 | `fetch` 为正文长度（已完整写入缓冲）；`read` 为本次拷贝字节数 |
| 1 | 参数非法（空源名、非 UTF-8 键/源名、坏 token） | 0 |
| 2 | 源未授权或未知（不发起请求） | 0 |
| 3 | 缓冲区不足 | 正文在该执行内缓存的读取 token，用 `query_read` 取回 |
| 4 | 查询失败（连接失败/超时/非 200/重定向/中途断连） | 0 |
| 5 | 查询次数或累计字节配额超限 | 0 |
| 6 | 非法客体内存范围（指针/长度越界） | 0 |

语义保证：

- 宿主固定 `GET` 固定地址，键经 URL 编码成为**单个**查询参数；程序不能
  影响路径、方法、头或重定向（`follow_redirects=false`，3xx 即查询失败）。
- 认证头由宿主附加，不暴露给程序、响应错误或日志。
- 正文必须**完整**才算成功：超长、`content-length` 不符、流式传输中断
  一律 `status=4/5`，不会把截断数据当成功返回。
- 缓冲区不足只在宿主内缓存正文并发放 token；`query_read` 分块取回，
  不会为了取长度而二次请求资料源。
- 查询等待计入原墙钟预算；看门狗在超时、调用方断连或服务关闭时直接
  关闭底层 HTTP 客户端，真实中止阻塞中的查询并中断执行、回收名额。
- 每次执行拥有独立的 broker/token/配额/缓存；不同执行间完全隔离。

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

### 本机资料源 + 真实 Wasm 补全示例

开两个终端（或后台启动）：

```bash
# 终端 1：本机资料源（要求固定 Bearer 令牌）
.venv/bin/python examples/device_spec_server.py --port 8091 --require-auth

# 终端 2：带命名资料源配置启动沙箱（凭据只在此进程环境中）
source examples/sources.env
.venv/bin/python -m uvicorn sandbox_service.app:app --host 127.0.0.1 --port 8081
```

```bash
.venv/bin/python examples/build_examples.py
M=$(base64 -w0 examples/enrich_device.wasm)
I=$(printf 'alpha-1\n' | base64 -w0)
curl -s -X POST http://127.0.0.1:8081/execute \
  -H 'Content-Type: application/json' \
  -d "{\"module\":\"$M\",\"stdin\":\"$I\",\"sources\":[\"device_specs\"]}"
```

程序以 8 字节缓冲发起 `query_fetch`（必然缓冲区不足），再用
`query_read` 分块取回完整 JSON；资料源只收到**一次** GET，且附带程序不可见
的 `Authorization` 头。返回的 `stdout` 解码后即补全后的设备规格：

```
{"model":"alpha-1","display":"7-inch","sensors":["temp","humidity"]}
```

未授权场景对照：不传 `sources` 时默认无网络，程序得到 status 2
（SOURCE_DENIED）且源服务零请求；传未知源 ID 直接 HTTP 400。

## 隔离与安全模型

- 每次请求使用全新的 Wasmtime `Engine`/`Store`/实例，互相不共享内存。
- 只连接标准流：stdin 来自请求体（内存），stdout/stderr 捕获到内存。
- 不调用 `inherit_env`/`inherit_stdin`/`inherit_stdout`，不 `preopen_dir`，
  环境变量显式置空，不预开放任何宿主目录或网络能力。
- 默认无网络；仅当请求显式列出已配置的源 ID 时，通过 `sandbox_queries`
  宿主模块获得能力最小的 GET 查询（见上节 ABI），地址/头/重定向由宿主固定。
- 仅允许导入 `wasi_snapshot_preview1` 固定白名单函数与 `sandbox_queries`
  的两个精确签名函数；导入其他模块、
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
| `sandbox_service/queries.py` | 每执行查询 broker：配额、超时、可中止 HTTP、正文缓存 |
| `sandbox_service/supervisor.py` | 并发名额、线程池、墙钟看门狗、查询中止、取消/关闭 |
| `sandbox_service/app.py` | FastAPI 路由与状态分类 |
| `examples/` | uppercase 等基础示例、enrich_device 查询补全、query_probe ABI 探针 |
| `examples/device_spec_server.py` | 本机演示资料源（可选 Bearer 认证） |
| `tests/` | HTTP、限制、并发隔离、查询 ABI/配额/中止自测 |

## 自测

```bash
.venv/bin/python examples/build_examples.py   # 生成 examples/*.wasm
.venv/bin/python -m pytest -q
```
