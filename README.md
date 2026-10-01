# Wasm Execution Sandbox

纯后端 HTTP 服务：接收第三方 WebAssembly 数据转换程序（WASI preview1），
在严格隔离的资源预算内运行其 `_start`，返回标准输出、标准错误、退出码或
明确的失败原因。没有前端，也没有模块仓库。

除了转换请求自带的数据外，程序还可以通过受控宿主调用，按设备型号等业务键
向运维方预先配置的资料源查询规格并补全记录。网络能力默认关闭、按执行显式
授予，程序永远拿不到地址或凭据。

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

本机资料源（示例设备规格库，监听 8090）：

```bash
.venv/bin/python examples/spec_server.py
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
| `allowed_sources` | string[]，可选 | 本次执行允许查询的资料源 ID；只能引用服务端已配置的源，默认空（无网络） |

`budget` 字段（只能小于等于 `GET /limits` 公布的服务端最大值）：

| 字段 | 默认 | 最大值 | 含义 |
| --- | --- | --- | --- |
| `fuel` | 10,000,000,000 | 10,000,000,000 | 指令燃料，耗尽即 trap |
| `timeout_ms` | 2000 | 5000 | 墙钟执行时间（毫秒），超时从另一线程真实终止 |
| `memory_bytes` | 16 MiB | 64 MiB | 线性内存上限 |
| `output_bytes` | 1 MiB | 4 MiB | stdout+stderr 合并总字节上限 |

查询预算（同样只能收紧）：

| 字段 | 默认 | 最大值 | 含义 |
| --- | --- | --- | --- |
| `max_queries` | 4 | 16 | 本次执行实际出站 GET 次数 |
| `query_response_bytes` | 64 KiB | 1 MiB | 单次响应正文字节上限 |
| `query_total_bytes` | 256 KiB | 4 MiB | 本次执行累计响应正文字节上限 |

其他固定上限：模块 16 MiB、stdin 4 MiB、活动执行数 8。
所有上限集中在 `sandbox_service/config.py`，可用 `SANDBOX_*` 环境变量覆盖。

## 受控资料源配置

资料源由服务端运维方命名配置，执行请求只传源 ID，绝不传地址或认证头。
两种配置方式（二选一）：

- 仓库根目录 `sources.json`；
- 环境变量 `SANDBOX_SOURCES`（内联同样的 JSON 字符串，优先级更高）。

格式：

```json
{
  "specs": {"url": "http://127.0.0.1:8090/spec"},
  "vendor": {
    "url": "https://vendor.example.invalid/api/v2/device-spec",
    "headers": {"Authorization": "Bearer REPLACE_WITH_TOKEN"}
  }
}
```

- 键即源 ID（非空、不超过 64 字符）；`url` 必须是 http(s) 绝对地址，路径固定。
- `headers` 可选，用于注入认证头等固定头；程序、响应错误与日志都接触不到它。
- 未配置任何源时，内置一个仅用于本机演示的 `specs`
  （`http://127.0.0.1:8090/spec`）；生产环境请显式配置。
- `GET /limits` 的 `sources` 字段只公布可用源 ID，不公布地址或认证头。
- 请求中出现未知源 ID 直接返回 400，且不会发起任何网络请求；未列入
  `allowed_sources` 的源在本次执行中同样不可用。

### 受控资料查询协议（宿主导入）

程序可从 `sandbox` 模块导入两个固定签名函数。其他模块名、函数名或不同的
参数/返回类型在执行前即被静态校验拒绝（见 `sandbox_service/validation.py`）：

- `query_fetch(src_ptr, src_len, key_ptr, key_len, id_out) -> i32`
  - `src` 为 UTF-8 源 ID，`key` 为 UTF-8 业务键，`id_out` 指向一个 i32，
    成功时写出响应句柄。
- `query_body(resp_id, dst_ptr, dst_len, copied_out) -> i32`
  - 用上一步句柄把完整正文拷入程序自己的缓冲区，`copied_out` 写出字节数。

宿主行为：

- 把 `key` 做百分号编码，作为唯一查询参数 `key` 拼到源固定地址，发起 GET
  （`<url>?key=<encoded>`）。程序不能控制方法、路径、请求头，也不能追加
  其它参数；宿主不跟随重定向，也不信任进程环境里的代理设置。
- 认证头仅由宿主在发送时附加，绝不返回给程序、写入错误信息或日志。
- 响应正文一次性完整取回（不会为“先探长度”而重复请求），保存在本次执行
  私有的宿主状态中，再由 `query_body` 拷入客体内存。

返回码（稳定且可区分）：

| 码 | 名称 | 含义 |
| --- | --- | --- |
| 0 | OK | 成功，`query_body` 已写入完整正文 |
| 1 | BAD_SOURCE | 源 ID 未知或本次执行未授权（不发起请求） |
| 2 | BAD_KEY | 业务键为空 |
| 3 | BAD_POINTER | 指针或长度越出客体内存 |
| 4 | BAD_UTF8 | 源 ID 或键不是合法 UTF-8 |
| 5 | FAILED | 连接失败、上游非 200 等查询错误 |
| 6 | TOO_MANY | 本次执行出站次数用尽 |
| 7 | RESPONSE_TOO_LARGE | 单次或累计响应字节超限 |
| 8 | BAD_CAPACITY | 句柄无效，或缓冲区放不下完整正文 |
| 9 | CANCELLED | 墙钟超时、客户端断连或服务关闭导致查询中止 |

完整性约定：缓冲区不足时返回 `BAD_CAPACITY`，绝不把截断正文当成功；任何
非 0 返回都不产生成功正文。所有指针先按当前客体内存做边界校验，非法指针
只得到 `BAD_POINTER`，不会造成宿主崩溃。每次执行使用全新的引擎、Store、
能力表与查询状态，授权源和响应正文都不跨请求共享。

查询等待计入原墙钟预算：剩余预算即本次 HTTP 超时。到点、断连或关闭时，
看门狗除推进 epoch 终止客体外，还通过中止钩子立即关闭进行中的 HTTP 连接，
被阻塞的宿主调用随之真实取消并回收名额。

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

### 受控查询补全示例（真实 Wasm + 本机资料源）

终端 1 启动资料源，终端 2 启动沙箱：

```bash
.venv/bin/python examples/spec_server.py                 # :8090
.venv/bin/python -m uvicorn sandbox_service.app:app \
  --host 127.0.0.1 --port 8081                            # :8081
```

`examples/device_enricher.wat`（源码）/ `.wasm`（已构建）逐行读取设备型号，
调用 `sandbox.query_fetch`/`query_body` 从 `specs` 源取规格，输出补全记录：

```bash
.venv/bin/python examples/build_examples.py   # 含 device_enricher

M=$(base64 -w0 examples/device_enricher.wasm)
I=$(printf 'TH-100
AX-7
NOPE
' | base64 -w0)
curl -s -X POST http://127.0.0.1:8081/execute \
  -H 'Content-Type: application/json' \
  -d "{"module":"$M","stdin":"$I","allowed_sources":["specs"]}"
```

`stdout`（Base64）解码后形如（未知型号得到可区分失败码，退出码为 1）：

```
TH-100	{"weight_g":320,"battery":"AAx2","rated_v":3.0}
AX-7	{"weight_g":980,"battery":"mains","rated_v":12.0}
NOPE	QUERY_FAILED:5
```

收紧查询上限的例子（只允许 1 次出站、单次正文最多 64 字节）：

```bash
curl -s -X POST http://127.0.0.1:8081/execute -H 'Content-Type: application/json' \
  -d "{"module":"$M","stdin":"$I","allowed_sources":["specs"],
       "budget":{"max_queries":1,"query_response_bytes":64}}"
# 第二条记录起为 QUERY_FAILED:6（出站次数用尽）
```

省略 `allowed_sources` 或传 `[]` 时，即使程序导入了 sandbox 函数也无法发起
任何请求（源 ID 解析为未授权，返回码 1，无网络出站）。

## 隔离与安全模型

- 每次请求使用全新的 Wasmtime `Engine`/`Store`/实例，互相不共享内存。
- 只连接标准流：stdin 来自请求体（内存），stdout/stderr 捕获到内存。
- 不调用 `inherit_env`/`inherit_stdin`/`inherit_stdout`，不 `preopen_dir`，
  环境变量显式置空，不预开放任何宿主目录或网络能力。
- 仅允许导入 `wasi_snapshot_preview1` 固定白名单函数，以及 `sandbox` 模块的
  `query_fetch`/`query_body`（固定签名）；导入其他模块、非函数导入、签名不符、
  缺少 `_start` 或非法 wasm 在执行前即被拒绝（400）。
- 网络默认完全关闭；仅当请求在 `allowed_sources` 中显式列出运维方配置的源 ID
  时，程序才能查询这些源。地址、路径、方法、请求头与凭据均由宿主掌握。
- 资源限制在执行前生效：fuel（指令数）、线性内存上限、墙钟超时、合并输出
  字节数，以及查询出站次数、单次/累计响应字节。墙钟与取消通过独立看门狗
  线程推进 engine epoch 触发真实 trap，并通过中止钩子立即关闭阻塞中的 HTTP
  请求；死循环、无限 `memory.grow`、持续输出、挂起的上游都会被终止。
- 并发数有硬上限；满载立即返回 503，无线程池之外的等待队列。客户端断连
  或服务关闭会取消并中断对应执行，槽位在 worker 的 `finally` 中恰好释放一次。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `sandbox_service/config.py` | 集中配置的默认值/最大值/并发度、命名资料源 |
| `sandbox_service/models.py` | HTTP 模型、预算收紧、sandbox 导入签名与结果码 |
| `sandbox_service/validation.py` | 模块编译与 WASI/sandbox 导入静态校验 |
| `sandbox_service/streams.py` | 有界 stdout/stderr 捕获与截断标记 |
| `sandbox_service/control.py` | 每执行的墙钟/取消控制与阻塞宿主调用中止钩子 |
| `sandbox_service/query.py` | 受控资料查询桥接：授权、次数/字节预算、单次 GET |
| `sandbox_service/wasi_instance.py` | 单请求 WASI 实例、stream/query host calls、`_start` 执行 |
| `sandbox_service/supervisor.py` | 并发名额、线程池、墙钟看门狗、取消/关闭 |
| `sandbox_service/app.py` | FastAPI 路由与状态分类 |
| `examples/device_enricher.wat` | 真实受控查询补全示例（逐行查规格） |
| `examples/spec_server.py` | 本机资料源（GET /spec?key=型号） |
| `examples/` | uppercase、busy_loop、memory_grower、spam_output、exits_nonzero 等 |
| `tests/` | HTTP、限制、并发隔离，以及查询授权/预算/指针/超时断连自测 |

## 自测

```bash
.venv/bin/python examples/build_examples.py   # 生成 examples/*.wasm
.venv/bin/python -m pytest -q
```
