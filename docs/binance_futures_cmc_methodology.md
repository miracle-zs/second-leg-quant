# Binance Futures 可交易 symbol 与 CMC 流通市值：方法核对

> 研究范围：仅核对 Binance Futures 与 CoinMarketCap 官方文档；不构成投资建议。本文只写方法，不读取或修改 `runs/` 下数据。

## 结论摘要

- Binance Futures 的 symbol 应以官方 `exchangeInfo` 返回的 `symbols[].symbol` 为准；“可交易”按对应产品的状态字段筛选为 `TRADING`。
- CMC 的流通市值应读取指定 CMC 资产的 USD quote 中的 `market_cap`；其口径是 CMC 参考价格 × 当前 circulating supply，而不是 total supply 或 max supply × 价格。
- Binance 合约 symbol 与 CMC 资产不是同一层级：应先用 Binance 的 `baseAsset` 识别 underlying，再通过 CMC `id` 做稳定映射；不要把 `BTCUSDT` 或 `BTCUSD_PERP` 整体当作 CMC symbol。
- 对 `1000X`、`1000000X`、`1MX` 等名义倍数合约，必须先还原 underlying，再用“合约价格 ÷ 倍数”与 CMC 价格做一致性校验；例如 `1000000BOBUSDT` 应映射到 Build On BNB，而不是同 ticker 的 BOB (Build on Bitcoin)。

## 1. Binance Futures：如何定义可交易 symbol

Binance USDⓈ-M 的 [Exchange Information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)（`GET https://fapi.binance.com/fapi/v1/exchangeInfo`）返回当前交易规则和 symbol 信息，包括 `symbol`、`pair`、`contractType`、`status`、`baseAsset`、`quoteAsset`、`marginAsset` 等字段。对 USDⓈ-M，可定义：

```text
tradable_usdm_symbols = {
    s.symbol for s in exchangeInfo.symbols
    if s.status == "TRADING"
}
```

Binance COIN-M 的 [Exchange Information](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Exchange-Information)（`GET https://dapi.binance.com/dapi/v1/exchangeInfo`）同样返回 `symbols`，但状态字段名为 `contractStatus`；示例还显示 `BTCUSD_200925` 这类交割合约的 `symbol` 与 `BTCUSD` 的 `pair` 是不同字段。因此：

```text
tradable_coinm_symbols = {
    s.symbol for s in exchangeInfo.symbols
    if s.contractStatus == "TRADING"
}
```

`contractType`（如 `PERPETUAL`、季度合约）应作为单独的产品筛选条件。若研究对象只要永续合约，应在上述交易状态筛选后再加 `contractType == "PERPETUAL"`，不要通过字符串切割猜测。做跨产品合并时保留 `market`（USDⓈ-M/COIN-M）、`symbol`、`pair` 和 `contractType`，避免同一 underlying 的多个合约被误当作多个币种。

## 2. 如何取得 CMC circulating market cap

推荐的两步流程：

1. 从 Binance `baseAsset`（例如 `BLZ`、`BTC`）取候选资产；调用 CMC [Cryptocurrency ID Map](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency) 的 `/v1/cryptocurrency/map`，将候选 symbol 解析为 CMC 的数值 `id`。CMC 官方明确建议在业务逻辑和后续 API 调用中使用 `id`，而不是 symbol。
2. 对已确认的 CMC `id` 调用 CMC [Quotes Latest](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency)（`GET https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/latest?id=...&convert=USD`），读取返回对象的 `quote` 中 `USD.market_cap`。该 endpoint 也返回 `circulating_supply`、`last_updated` 和 `is_market_cap_included_in_calc` 等字段，建议一并保存以便审计；若批量查询，核对返回的每个 `id` 是否都存在。

CMC [Market Data](https://support.coinmarketcap.com/hc/en-us/articles/360034116491-Market-Data) 给出的公式是：

```text
circulating_market_cap = CMC reference price × current circulating supply
```

因此，`quote.USD.market_cap` 是首选结果；只有在需要交叉校验时，才用同一响应中的 CMC 价格与 `circulating_supply` 重算，并保留时间戳。不要用 `fully_diluted_market_cap`、`minted_market_cap` 或自算的 Binance 合约名替代它。

## 3. CMC 口径、缺失与映射陷阱

- **口径不是简单的总发行量。** CMC [Supply (Circulating, Total, Max)](https://support.coinmarketcap.com/hc/en-us/articles/360043396252-Supply-Circulating-Total-Max) 将 circulating supply 视为市场和公众手中的最佳近似，类比股票 public float；锁定、内部人、团队/基金会/金库、私募等持仓通常不计入。CMC 会结合项目沟通、区块链和分配表核验，且明确可能不直接采用项目 API、白皮书或博客中的数字。
- **自报值不能当作 CMC verified 值。** 同一官方页面区分 `self-reported_circulating_supply` 与 CMC 核验值，并说明自报数据可在页面并列展示、但不影响排名。若主流程要“CMC circulating market cap”，应以 `quote.USD.market_cap` 和 CMC 的 circulating supply 为准，并把 self-reported 字段单独标记。
- **缺少市值不等于没有 Binance 合约。** CMC 的 ID Map 默认返回在支持交易所上有 actively tracked markets 的资产，也可显式请求 `inactive` 或 `untracked` 状态；因此 map 无匹配、无 verified market cap、或 `is_market_cap_included_in_calc` 不纳入计算，都应保留为不可用/待人工复核，而不是填 0 或用 total supply 代替。CMC 的 [Ranking](https://support.coinmarketcap.com/hc/en-us/articles/360043836851-Ranking-Cryptoasset-Market-Pair-Exchange) 也明确存在“无 verified market capitalization、按 24h volume 排序”的 unranked 资产。
- **symbol 不是唯一键。** CMC [Standards and Conventions](https://coinmarketcap.com/api/documentation/guides/standards-and-conventions) 警告 cryptocurrency symbol 可能重复，也可能因 rebrand 改变；按 symbol 查询多重匹配时 API 默认最高市值项目。故应把 Binance `baseAsset` → CMC 候选集合 → 人工/规则确认的 CMC `id` 持久化，不能直接 `symbol -> market_cap`。
- **代币需要进一步确认链与合约。** 对同 ticker 的多链代币，除 `baseAsset`/symbol 外，应核对 CMC map/info 返回的 `platform`、`token_address`、项目名称/slug；若没有足够信息确认唯一资产，标为 ambiguous，不要强行映射。
- **请求参数可能掩盖缺失。** CMC Quotes Latest 的 `skip_invalid=true` 会跳过无效 lookup 并继续返回其余资产；批量流程必须对请求 ID 与响应 ID 做集合差集检查，并记录缺失原因。Listings Latest 是分页的 active-asset 列表，不能只取默认第一页后就认为其余 Binance underlying 不存在；少量已知资产优先使用 Quotes Latest。

## 建议的落地字段

```text
binance_market, binance_symbol, binance_pair, contract_type,
binance_base_asset, binance_status,
cmc_id, cmc_name, cmc_symbol, cmc_platform, cmc_token_address,
cmc_circulating_supply, cmc_usd_market_cap, cmc_is_market_cap_included,
cmc_last_updated, mapping_status, mapping_note
```

其中 `mapping_status` 至少区分 `matched`、`missing_cmc`、`ambiguous_symbol`、`untracked_or_unranked`；任何缺失都保留为缺失，不用 0 伪造市值。
