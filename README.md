# GPUDeck CLI

面向 AI 和终端的 [GPUDeck](https://gpudeck.cslee.cc) 预约客户端。Python 3.10+，无第三方运行时依赖，直接调用平台的 `/api/v1` 接口，不需要浏览器。

## 安装

```bash
uv tool install 'git+https://github.com/chimeraHHH/gpudeck-cli.git'
gpudeck --version
```

开发安装：克隆仓库后，在项目目录运行 `uv tool install --editable .`。也可以创建虚拟环境后 `python -m pip install .`。不安装即可在此目录执行 `python3 -m gpudeck_cli ...`。

## 登录一次，随后一条命令预约

```bash
gpudeck login --username YOUR_USERNAME
# 在终端隐藏输入密码；之后的命令自动复用登录会话。

gpudeck reserve \
  --server WHUServer-H200 --gpus 0 \
  --start "2026-10-05 09:00" --hours 4 \
  --project "模型训练" --purpose "训练"
```

示例时间需替换成实际所需时段。未指定 `--start` 时使用平台当前时间；不含 UTC 偏移的时间默认按 `Asia/Shanghai` 解释，与运行机器的时区无关。可用 `--timezone` 更改，或直接传 `2026-10-05T09:00:00+08:00`。

完全非交互使用：由运行环境注入 `GPUDECK_USERNAME`、`GPUDECK_PASSWORD`，然后直接运行 `gpudeck reserve ...`。没有会话或会话过期时自动登录。也支持 `gpudeck login --username USER --password-stdin`，从标准输入读取一行密码。密码没有命令行参数，不写入配置、Cookie 文件或输出；请不要把真实密码写进代码和命令历史。

可以让 CLI 选择卡：

```bash
gpudeck reserve --auto --model H200 --count 1 \
  --start "2026-10-05 09:00" --hours 4 \
  --project "模型训练" --purpose "训练" --dry-run
```

`--auto` 只在指定筛选条件内选择可预约的 GPU，优先选择采样新鲜且当前实际空闲的卡。可同时指定 `--server`。需要固定卡时使用 `--server ... --gpus 0,1`，跨服务器使用重复的 `--gpu-id ID`。删去 `--dry-run` 即提交。

## 查询和管理

```bash
gpudeck whoami --pretty
gpudeck gpus --server H200 --pretty
gpudeck gpus --idle
gpudeck processes --mine
gpudeck reservations --mine --status scheduled
gpudeck reservations --from "2026-10-05 09:00" --to "2026-10-05 13:00"
gpudeck availability --server H200 --start "2026-10-05 09:00" --hours 4
gpudeck cancel RESERVATION_ID --dry-run
gpudeck cancel RESERVATION_ID
gpudeck end RESERVATION_ID
gpudeck logout
gpudeck schema --pretty
```

`reservations` 默认返回平台可查询的全部预约，历史范围由服务器决定。`--mine` 按用户 ID 过滤；进程的 `--mine` 按平台绑定的 Linux 用户名过滤。`gpus --idle` 只说明当前实际占用，预约时段是否可用应查询 `availability`。

## AI 输出协议

所有正常结果和错误都输出到标准输出，每次调用一个 JSON 对象：

```json
{"ok":true,"data":{"created":true,"reused":false,"verified":true,"reservation":{"id":"..."}}}
```

```json
{"ok":false,"error":{"code":"conflict","message":"...","status":409}}
```

`--pretty` 开启缩进；`--json` 可显式声明 JSON 模式。`--help` 和 `--version` 是普通终端文本。使用 `gpudeck schema` 读取命令及输出说明，或参阅 [AI_USAGE.md](AI_USAGE.md)。

| 退出码 | 含义 | AI 下一步 |
| --- | --- | --- |
| 0 | 成功 | 读取 `data` |
| 1 | 网络、平台或本地错误 | 检查错误类型；读取类请求可适当重试 |
| 2 | 参数错误 | 修改参数 |
| 3 | 登录或权限错误 | 重新登录或检查账号 |
| 4 | 预约冲突、无可用卡或校验失败 | 调整 GPU 或时间 |
| 5 | 提交结果不确定，或提交后无法验证 | 先查询预约记录，勿直接重复提交 |

## 预约行为

- 创建前检查 GPU、时间、已有预约、并发数量和项目/用途长度；平台仍执行最终校验，处理检查后发生的竞态。
- 默认遵循当前平台的单次最长 48 小时、最多提前 14 天、可从当前半小时窗口开始的规则；并发上限由 `whoami` 获取。
- 对同一账号、GPU、明确起止时间、项目和用途的现存预约返回 `reused: true`，不再次提交。两次 `--start now` 的时间不同，不算完全相同的请求；并发竞态最终由平台阻止重叠。
- POST 请求不自动重试。超时、断线、提交后 5xx 或异常响应会报告 `outcome_unknown`；创建后查询失败会保留 `reservationId`。
- 创建、取消、提前结束后均查询预约列表验证结果。重复取消或结束已处于目标状态的记录返回 `changed: false`。
- 当前有进程或显存占用会作为 `check.warnings` 返回。离线、过期采样或缺失显存数据不会被当成实际空闲。
- 预约是平台的排期记录。它不会停止进程，也不会启动训练；有预约不等于已获得独占的 GPU。

## 凭据和配置

默认状态路径为 `~/.local/state/gpudeck/<平台地址哈希>/cookies.txt`，或使用 `XDG_STATE_HOME`。各平台地址的会话隔离，目录权限 `0700`，Cookie 文件权限 `0600`。Cookie 是会话凭据；密码不持久化，CSRF Token 不输出。远程 URL 必须使用 HTTPS，并拒绝自动重定向；HTTP 仅支持本机接口测试。

| 配置 | 环境变量 | 参数 |
| --- | --- | --- |
| 平台地址 | `GPUDECK_URL` | `--url` |
| 用户名 | `GPUDECK_USERNAME` | `--username` |
| 密码 | `GPUDECK_PASSWORD` | `--password-stdin` |
| 状态目录 | `GPUDECK_STATE_DIR` | `--state-dir` |
| 时间解析时区 | `GPUDECK_TIMEZONE` | `--timezone` |
| 请求超时 | — | `--timeout`，默认 20 秒 |

改密码后重新运行 `gpudeck login --username YOUR_USERNAME` 即可。退出登录只注销本 CLI 的会话。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用本机 HTTP 契约服务，不需要真实账号，涵盖 Cookie/CSRF、会话复用与过期、账号切换、预约/取消/结束、重复请求、冲突、提交后超时和验证失败、时区、并发和凭据输出隔离。真实平台测试记录见 [VALIDATION.md](VALIDATION.md)。

协议依据：部署页面公开的前端请求，以及上游 [GPUDeck Hub API](https://github.com/leeechsh/gpudeck/blob/main/crates/gpudeck-hub/src/api.rs) 和 [预约检查逻辑](https://github.com/leeechsh/gpudeck/blob/main/src/hub/reservationChecks.ts)。这是独立 CLI；平台未来更改接口或规则时需要同步调整。

## 许可证

[MIT](LICENSE)。
