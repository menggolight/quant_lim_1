# Tushare 历史回填与 Alpha Feasibility P1

本轮只回答一个前置问题：在暂不扩建九类正式执行数据和小账户成交仿真的情况下，既有六因子 Technical Alpha 与冻结 Exposure 是否值得继续建设完整执行层。它是独立的 `research_alpha_feasibility_only` 研究门，不替代现有正式小账户框架，不授予 Paper、交易或 LIVE 权限。

## 冻结边界

- 市场、策略、请求参数和消费者数据日期只允许 `2017-07-01..2023-12-31`；Development 为 `2018-01-01..2022-12-31`，Validation 为 `2023-01-01..2023-12-31`。`generated_at` 仅记录真实审计生成时点，不是市场数据、信号日期或 Locked Test 日期，任何消费者不得将其解释为特征。
- 2024—2025 Locked Test 在配置、请求、缓存、DataFrame、消费者和报告层均不可达，固定为 `NOT_ACCESSED / NOT_DOWNLOADED / NOT_RUN`、`locked_test_consumed=false`。
- P1.3 仅允许标准只读 `trade_cal`、`index_weight`、`daily`、`adj_factor`、`index_daily`、`suspend_d` 六个接口；`stock_basic` 延期且请求数固定为0，不下载当前快照，也不建设历史时点 security master。仍不用 `pro_bar`、`*_vip`、Choice、券商、账户或订单接口，也不做字段级 BaoStock fallback。
- 六因子、权重、entry/hold 门槛、Exposure 阈值、最多3只、单只40%及0/30/60/100%总仓位均由源文件 SHA-256 绑定，运行时再次核验实际 ranker 与 Exposure 源码。

## 数据门

标准入口先逐月请求 `000906.SH` 的 `2017-12..2023-12` 共73个 `index_weight` 窗口。唯一网络进程必须先单独完成2017-12首月的字段语义与PIT截面校验，只有首月通过才继续其余72个月，且不得重复首月请求。每月必须存在合法截面、代码唯一、权重非负且权重和落在非零权重最粗服务端末位精度的半单位容差内；行数和零权重不得放大总和容差。成分数不是800时，只有绑定中证指数公司正式证据、月份、截面日、实际数量、原因和源文件哈希的受控说明才可放行；否则阶段状态为 `BLOCKED_PIT_MEMBERSHIP`，最终状态为 `BLOCKED_DATA`，不会规划股票历史任务。

上述混合精度与新容差语义由 `pit-membership-coverage-report.v2` / `pit-membership-manifest.v2` 承载；历史V1文件保留用于识别旧产物，但当前生产端、loader和策略消费者均显式拒绝V1，禁止把旧容差证据静默解释为V2。

PIT 通过后，只对73个月合法截面的成员并集回填。每个决策日只能选择当日或之前最新合法 `index_weight` 截面；`con_code` 必须是沪深A股格式，并与 `daily.ts_code` 并集精确一致。不得用当前成分、`stock_basic` 或其他当前快照回填历史。所有请求在本地形成不含 Token 的参数指纹、started claim、规范化响应哈希和 create-only 产物；完整任务离线重放，已开始但没有持久化响应的远端调用按歧义失败关闭，不自动重发。上游响应或错误字段一旦出现 `2024-01-01` 及以后日期，在进入消费者前隔离且不保存原始正文。

Tushare HTTP 根包络分为严格 `semantic_core` 与受控 `transport_extensions`。`semantic_core` 必须且只能按语义解释 `code/msg/data`：根必须是无重复key的JSON object，`code`必须是int且不接受bool，`msg`只能是string或null；`code=0`时`data`必须是object，`code!=0`时`data`只能是null或受控错误object。缺少核心字段或类型漂移一律失败关闭。非零`code`仍须按结构化code、msg和HTTP状态进入权限、限频、账户认证、参数、服务端或未知上游错误分类；扩展字段存在不能绕过非零`code`。

成功响应的 `data` 必须包含 `fields/items`；仅额外允许本地证据已经观察并收紧类型的分页元数据：`has_more` 必须严格为 `false`，`count` 必须严格为整数 `0`。它们均不进入规范化行或策略内容；`has_more=true` 仍按潜在截断失败关闭，其他值或其他 `data` 扩展继续失败关闭。`data.fields` 是非空、唯一、安全字段名数组，字段顺序不承载业务语义。每行宽度必须与服务端实际 `fields` 一致，适配器先按服务端字段名和位置建行对象，再投影到请求契约的固定规范字段顺序；缺必需字段、重复字段、非法字段名、行宽错误或必需值非法均失败关闭，不按固定列位置解析。`index_weight` 的必需/规范字段固定为 `index_code/con_code/trade_date/weight`，规范化行按 `index_code/trade_date/con_code` 排序。服务端额外列允许存在，但只记录列名和逐列内容SHA-256，不进入规范化行、PIT成员、策略输入或内容哈希。

### P1.4D 单次值诊断证据

P1.4D 只执行了一次固定的 `index_weight(index_code=000906.SH,start_date=20171201,end_date=20171231)` 请求；其他Tushare接口均为0次。通过Token、敏感字段、非有限JSON、日期、极端Decimal指数和2 MiB上限扫描后，完整正文以create-only方式保存在忽略Git的诊断目录。响应为34510字节、800行，服务端字段精确为 `index_code/con_code/trade_date/weight`，主键无重复；`index_code/con_code/trade_date` 均为合法字符串，`weight` 800行均为JSON number，无null、bool、负数或零值。

冻结基线 `a0fadfc890d26be16e1d4c06e556674a59aa4be6` 的首个具体失败位于0-based第7行：`weight=1.23`，谓词为 `weight_decimal_scale_below_three`。完整画像显示源精度分布为1位6行、2位67行、3位727行，截面权重和为 `99.997`。因此P1适配器不再要求人为的至少3位小数：JSON integer/number及普通十进制数字字符串先按非负Decimal校验并保留服务端末位精度；bool、null、非有限数、负数、signed zero、指数或带正号的字符串继续拒绝。权重和容差取非零权重最粗服务端末位精度的半单位，且由策略消费者独立重算；零权重沿用既有“合法非负值且不得静默删除”的规则，但不扩大总和容差。本次真实 `zero_count=0`，未引入零值过滤或补值。

同一份原始响应在修复后离线重放两次，均得到800行、唯一日期 `20171229`、权重和 `99.997` 及相同规范字节和内容哈希，终态为 `DIAGNOSTIC_REPLAY_ACCEPTED`。本轮没有恢复其余72个月、历史行情、Development、Validation或Locked Test，也不构成Alpha统计有效、Paper或交易准入。

除三项核心字段外的所有顶层字段统一进入 `transport_extensions`，不维护 `request_id/detail/...` 固定可选白名单。安全非空字段名及其合法JSON标量、数组或object可被接受，因此 `request_id`、string/object/array形态的`detail`和未来`trace_id`本身不会触发 `BLOCKED_ADAPTER_PROTOCOL`；但扩展及其嵌套key仍须通过控制字符、secret-like key、当前`TUSHARE_TOKEN`精确内容、非有限数和纯JSON类型检查。完整响应最多2 MiB，扩展规范化对象最多256 KiB，扩展最多64个根字段、4096个总元素、8层嵌套，单字符串最多65536字符。任何上限或secret检查失败都在解释上游错误前失败关闭。

传输与内容身份分成四层：`raw_transport_sha256`绑定完整原始响应正文，`transport_extensions_sha256`绑定扩展规范化对象，`provider_payload_sha256`绑定服务端实际 `fields/items`，`normalized_content_sha256`只绑定规范投影后的固定字段和领域规范值。相同业务数据只改变字段顺序，或只增加/改变额外列时，`provider_payload_sha256`按实际payload变化，`normalized_content_sha256`必须保持不变；`request_id`、`detail`、额外列及未来扩展不得进入normalized rows、PIT manifest或Experiment内容哈希。

普通transport receipt只保存观察到的根字段、三项核心字段、扩展字段名/JSON类型/逐值SHA-256、扩展整体SHA-256与字节数、完整传输SHA-256及Token泄漏检查，不保存扩展原值。task response另绑定实际/必需/缺失/额外字段、规范顺序诊断、服务端payload哈希、额外列逐列哈希、规范内容哈希与行数；这些审计字段不能反向进入策略数据。完整原始正文只有通过严格Token/secret扫描后才允许进入忽略Git的create-only原始证据目录；存在扩展时，普通重放证据剥离根扩展但保留实际 `data.fields/items` 供字段证据复核。已封存的旧response/receipt只允许原位只读重放，不升级或改写。quarantine同样只保存安全化字段诊断、哈希、行数、稳定失败码和受控上游分类，不输出完整`detail`、request ID、Token、Authorization、Cookie或响应正文。

适配器协议的外层仍以稳定安全码封装；`data` 内层失败分类固定使用 `data_fields_not_array`、`data_field_name_invalid`、`data_duplicate_fields`、`data_required_fields_missing`、`data_item_width_mismatch`、`data_required_value_invalid`，不再使用模糊的字段集合/顺序不一致错误。字段顺序变化和额外列只形成诊断，不阻断。`upstream_permission_error`、`upstream_rate_limit_error`、`upstream_authentication_account_error`、`upstream_invalid_parameter_error`、`upstream_server_internal_error`和`upstream_unknown_error`表示核心与扩展契约已经通过后的上游拒绝，归入`BLOCKED_DATA`而不是适配器协议漂移。

历史完整性要求：

- `trade_cal` 覆盖每个自然日并校验 `pretrade_date`、年度开市日数量和窗口内下一交易日映射；末日不跨到2024。
- `daily` 保存未复权 OHLCV 及单位；`adj_factor` 只做当日或之前 as-of；`index_daily` 完整覆盖每个开市日。
- 每个决策日的每个PIT成员都必须有且只有一个历史资格结论。至少121个有效受控交易日才进入冻结Alpha ranker；不足121日只记 `eligibility=false / reason=ineligible_insufficient_history`，不要求Alpha分数，也不阻断整个截面。
- 首次纳入即全时段停牌且没有任何初始有效价格时，记 `eligibility=false / reason=ineligible_no_initial_price`。上市前、首次停牌或无初始价格日期不补0、不前向填充；已有经济价值后的停牌才可凭同日 `suspend_d` 冻结沿用。已有足够历史后的非停牌异常缺口继续记 `unexplained_market_data_gap` 并失败关闭，也不跨 Provider 补字段。
- `stock_basic_status=DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY`、`stock_basic_request_count=0`、`security_master_pit_status=NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1` 是配置、manifest、CLI 和最终报告的固定契约；`stock_basic` 缺失本身不得成为 `BLOCKED_DATA`。

正式数据产物位于每轮新的、忽略提交且create-only的 `data/tmp/alpha-feasibility/<fresh-p1-root>/`，其中至少包括：

- `pit_membership_coverage_report.json`
- `pit_membership_manifest.json`
- `history_coverage_report.json`
- `history_manifest.json`
- `alpha_feasibility_report.json`

最终报告把 collection plan、PIT manifest、history manifest、冻结实验 canonical config、Alpha Feasibility 引擎源码及终态 Gate 源码的 SHA-256 一并纳入自哈希。标准 loader 会从 create-only task store 重建历史 coverage/manifest 并逐字比对，不能只改 manifest 后重签自哈希。若在任何网络调用前因 Token 或本地证据失败，CLI 仍发布无指标的 `BLOCKED_DATA`，并用独立 create-only evidence 固定时间戳以支持字节一致重放。

## 收益、时序与成本

股票信号经济价值为 `raw_close_t * latest_adj_factor_available_on_or_before_t`；开盘执行参考为 `raw_open_t * 同一因果因子`。停牌日无论供应商是否同时返回 bar，信号 open/high/close 均沿用前一经济价值，并要求同日 `suspend_d` 证据。内部非停牌缺口会在排名前阻断，不能静默变成横截面排除。

时序固定为 D 收盘计算 Alpha/Exposure，D+1 未复权开盘的调整后同口径值进行小数权重换仓：旧仓承担 D 收盘到 D+1 开盘段，新目标只承担 D+1 开盘到收盘段。该模型扣除基础/压力双情景的比例佣金、卖出税、过户费和滑点，`annualized_turnover` 使用单边口径；最低5元佣金、整手、精确分红/送股股数、涨跌停、ST、退市终值和券商成交均未模拟，因此始终 `execution_realism=INCOMPLETE`。

## 运行与终态

```powershell
python -m operations.run_alpha_feasibility all `
  --config configs/a_share_technical_alpha_feasibility.v2.json `
  --output-root data/tmp/alpha-feasibility/tushare-p1-v2
```

`data` 子命令只完成数据门；默认 `all` 在数据验签通过后运行 Development/Validation 的 base/stress。禁止参数搜索、按2023结果重训或调整阈值。终态只有：

- `ALPHA_FEASIBILITY_GO_CANDIDATE`：Validation base 主动净收益大于0、stress 不小于0、两情景最大回撤均不超过12%，且单股与最佳10日收益集中度均不超过预注册50%门。
- `ALPHA_FEASIBILITY_NO_GO`：数据完整，但冻结 Alpha 在 Validation 不值得继续扩建完整执行层。
- `BLOCKED_DATA`：PIT、历史、回放或消费者完整性未通过；报告不含 Development/Validation 指标。
- `BLOCKED_ADAPTER_PROTOCOL`：Tushare HTTP/JSON包络或data结构违反严格适配器契约；报告不含 Development/Validation 指标。

即使为 GO candidate，也只是后续工程候选，不是实盘收益、Paper 准入、股票推荐或交易授权。

最终报告还固定输出首个 `index_weight` 的字段投影/双内容哈希证据，以及 `stock_basic_status`、`stock_basic_request_count`、`security_master_pit_status`、`valid_candidate_count_by_decision`、`insufficient_history_count_by_decision`、`ineligible_no_initial_price_count` 与 `unexplained_market_data_gap_count`；Locked Test 三项状态保持 `NOT_ACCESSED / NOT_DOWNLOADED / NOT_RUN`，`locked_test_consumed=false`。

为避免 IEEE-754 舍入把极小越界误判为通过，报告中的收益率、回撤、换手、成本、Exposure 和集中度均以 canonical decimal string 持久化；门禁使用 `Decimal` 精确比较。
