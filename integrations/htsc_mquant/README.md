# 华泰 MQuant 只读影子快照导出器

`htsc_shadow_exporter.py` 是运行在官方 MATIC/MQuant 客户端内部的只读策略脚本。它查询资金、持仓、未完成委托、当日委托与当日成交，并将单个 JSON 快照原子替换到本机文件。脚本没有交易写入能力，输出也明确声明：

```json
"capabilities": {
  "read_only": true,
  "orders_enabled": false
}
```

这不是互联网 REST API，也不绕过华泰客户端的登录、授权或风控。登录凭据只留在官方客户端中，不应写入脚本、运行参数、JSON 或本项目配置。

## 使用前必须校准当前客户端契约

本实现依据华泰公开的《MATIC-MQuant 使用手册 V3.1》，该公开手册版本日期为 2021-01-27，不能视作 2026 年当前客户端契约。首次运行前，必须在已获授权的当前 MATIC 客户端中打开“工程管理 → MQuant → 帮助文档”，对照客户端自带的 `MQuant_api.py` 和 `MQuant_struct.py`，逐项确认：

1. `get_fund_info`、`get_positions_ex` 的参数和返回结构；
2. `get_open_orders_ex`、`get_orders_ex`、`get_trades_ex` 的参数顺序与返回三元组；
3. 三个扩展查询的第二个返回值仍是 `is_last`，并且必须分页到 `is_last=true`；
4. `FundUpdateInfo`、`Position`、`Order`、`Trade` 的字段名仍与映射一致；
5. `run_timely` 的回调签名与时间间隔单位；
6. 普通 A 股账户类型仍接受 `stock`，或按当前 SDK 要求调整 `account_type`。

任一项不一致，应先修改映射并在只读环境验证，不能把不完整快照接入策略账本。

取得当前客户端文件后，可先在本项目运行（只做静态读取，不执行 SDK）：

```powershell
python integrations/htsc_mquant/inspect_local_sdk_shape.py <MQuant_api.py路径> <MQuant_struct.py路径>
```

输出的 `shape_checked=true` 与 `local-shape-sha256:<64位哈希>` 只表示两个本地文件具备当前映射所需的名称，并用于发现文件变化。它不证明文件来自华泰、不证明是当前版本，也不证明 MQuant 运行时实际加载了这两个文件；即使手写伪文件也可能通过。因此它只能登记为导出器的 `API_SHAPE_ID` 与外部配置的 `expected_api_shape_id`，不能称为“SDK/契约已验证”。官方来源、客户端版本和实际运行回包仍必须人工核验。

公开手册：<https://s3.cn-north-1.amazonaws.com.cn/htscweb/huatech/attachment/MATIC-MQuant%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8CV3.1.pdf>

## 配置

在 MQuant 提交策略时传入以下运行参数。路径必须是绝对路径；示例路径仅供本机使用：

```json
{
  "snapshot_path": "C:\\path\\to\\quant\\data\\broker\\htsc_mquant_shadow.v1.json",
  "account_binding_id": "htsc-local-0123456789abcdef0123456789abcdef",
  "account_binding_secret": "请在本机生成至少32字符的随机值且不要写入项目",
  "account_type": "stock",
  "interval_seconds": 5,
  "page_size": 500
}
```

`account_binding_id` 必须是本机随机生成的绑定 ID，格式为 `htsc-local-` 加 32 位小写十六进制字符。它不是、也不能由资金账号、股东账号、手机号或身份证号派生。可在本机生成一次：

```powershell
python -c "import uuid; print('htsc-local-' + uuid.uuid4().hex)"
```

`account_binding_secret` 是只输入 MQuant 运行参数的本机随机秘密，用来对当前实际资金账号计算 HMAC 指纹；原始资金账号和该秘密都不会写入快照。切换登录账户后，指纹会变化，外部适配器会拒绝旧绑定。首次快照必须由用户在 MATIC 界面人工确认账户后，再把输出的 `source.account_fingerprint` 登记为预期指纹。不要把这个秘密提交到 Git、聊天或项目配置。该指纹只能在“诚实运行此导出器”的前提下检测账户切换；快照里包含指纹本身，所以它不能认证文件来源，也不抵抗能改写快照的本机进程。

如果当前客户端无法传入自定义参数，可在脚本顶部填写同名常量；同样不得填写任何真实账户标识。`session_id` 由每次脚本启动随机生成。

## 运行方式

1. 用户自行登录官方 MATIC 客户端，不向本项目提供密码或验证码。
2. 在 MQuant 中新建 Python 策略并导入脚本；若当前客户端限制策略文件名长度，可在导入时复制成更短的本地文件名。
3. 仅勾选需要读取的普通 A 股账户，并设置上述参数。
4. 启动后，脚本先立即导出一次，再通过 `run_timely` 周期导出。
5. 先在 MATIC 页面与 JSON 之间人工核对资金、持仓、委托和成交；核对通过前，下游只能把它作为 Shadow 数据源。

导出器只使用以下官方只读/定时接口：

- `get_fund_info`
- `get_positions_ex`
- `get_open_orders_ex`
- `get_orders_ex`
- `get_trades_ex`
- `run_timely`

## 完整性与隐私约束

- 三个分页查询即使返回空页，也会继续请求，直到 `is_last=true`。
- 三个查询均按账户级无筛选语义实现；当前实现强制 `reported_total_count == returned_count`，不一致即判为不完整。该语义必须用当前 SDK 校准；如果当前 SDK 定义不同，应先修改并重新测试契约，不能静默放宽。
- 分页过程中总数变化、重复 ID、字段缺失、非有限数值或任一查询异常，都会令相应 `capture.sections` 为 `false`，并令 `capture.complete=false`。
- `capture.complete=true` 只表示五类查询与分页检查分别成功；这些查询是顺序执行的，不是券商原子快照，明确标记为 `capture.consistency=sequential_non_atomic`。成交或撤单恰好发生在查询窗口内时，区段可能来自不同瞬间，因此当前快照不能直接驱动策略账本或交易。
- 失败快照仍会原子写出，便于下游明确拒绝；资金失败时的零值只是占位，绝不可当作真实账户值。
- 只映射白名单字段，明确跳过 MQuant 对象中的资金账号、股东账号、交易员等字段；错误详情也不写入 JSON，避免经异常消息泄露账户标识。
- 委托的 `cancel_info` 会遮蔽连续 8 位以上的数字标识。
- 输出目录的存在性检查/创建只在初始化配置阶段执行一次；周期回调不读文件，只执行同目录临时文件写入、`flush`、`fsync`、`os.replace`，下游不会读到半个 JSON。

## Schema 与哈希

Schema 为 `htsc-mquant-shadow/1`。顶层包含：

- `schema_version`
- `capabilities`
- `source`
- `capture`
- `funds`
- `positions`
- `open_orders`
- `today_orders`
- `trades`
- `payload_sha256`

`payload_sha256` 的计算规则固定为：先移除顶层 `payload_sha256`，再执行

```python
json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
).encode('utf-8')
```

最后计算 SHA-256，并保存为 `sha256:<64位小写十六进制>`。下游应先验证哈希、Schema、绑定 ID、时效、`capture.complete`、`capture.consistency` 和只读能力标志，再读取数据。

该 SHA-256 没有密钥，只能发现截断和误改，不是来源认证。快照目录应限制为当前 Windows 用户可写；即便如此，本桥接器仍将 `source_authenticated=false`，它不能作为未来实盘权限证明。
