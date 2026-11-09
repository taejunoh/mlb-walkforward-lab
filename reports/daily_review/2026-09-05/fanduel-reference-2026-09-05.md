# FanDuel reference capture — 2026-09-05

Observed 2026-09-05 21:39:48–21:39:49 UTC (17:39:48–17:39:49 EDT), immediately after the requested ~17:34 ET snapshot. Source was the official FanDuel NY regional event API:

`https://sbapi.ny.sportsbook.fanduel.com/api/event-page`

Reusable public parameters: `betexRegion=GBR`, `capiJurisdiction=intl`, `currencyCode=USD`, `exchangeLocale=en_US`, `includePrices=true`, `language=en`, `priceHistory=1`, `regionCode=NAMERICA`, `_ak=[REDACTED_PUBLIC_APP_KEY]`, `eventId={event_id}`. The `_ak` value is redacted from this review artifact. It is a public app configuration key, not a private credential.

All records below are full-game `MONEY_LINE` plus `TOTAL_POINTS_(OVER/UNDER)` markets. API responses were checked for `marketStatus=OPEN` and `inPlay=false`. Fetch time is observation time only; this artifact does not claim a backend last-price-update time.

| Event | Away ML | Home ML | Total | Over | Under | Raw response SHA-256 |
|---|---:|---:|---:|---:|---:|---|
| 36030294 ATL @ PHI | +138 | -164 | 8.5 | -104 | -118 | `7539ecbd8cee9c097ca9346c6f99e1b5902d9243897cd6cc4ef5cb8a33065ee9` |
| 36030289 DET @ CLE | +134 | -158 | 7 | -102 | -120 | `c94098085460e2bfa82a3badd178152b308c81e17f046821a296556f14aa2df3` |
| 36030291 LAA @ PIT | +154 | -184 | 7.5 | -118 | -104 | `6d12351327db7c32a82dfdbc8f8d234be3b8f8c274914d66627fd49540e2b49d` |
| 36030295 MIL @ CIN | -156 | +132 | 9 | -114 | -106 | `649a831dd33c83f9adf7722403663e1b37c2a6874fb70011b991af6fdf7c94d8` |
| 36030298 BOS @ BAL | -132 | +112 | 7.5 | -122 | +100 | `910d7614d28e7a328433cb8b18c1ee37caa07fbb557182af92c12cf1d63d6399` |
| 36030287 TB @ TEX | -108 | -108 | 6.5 | -124 | +102 | `d0cea3e6884d913fab7b496e1665152feff0c05c87a7da5d6317e2982b5fd62f` |
| 36030296 TOR @ KC | -106 | -110 | 9.5 | -105 | -115 | `3143afc6bb8fa6768fa8d39795a8abba2abc5b02e12a02cfd77531ad7292db88` |
| 36030297 MIN @ CWS | +106 | -124 | 8.5 | +100 | -122 | `d13d577536aac5b16feda68e4ff84aa843205ada847461bf27be9b8154623eb5` |
| 36030288 ARI @ HOU | -108 | -108 | 8.5 | -124 | +102 | `5e82961d7439b5ffc577d0b75852979173fbe84582f7da263ab5a2dfc21951ff` |
| 36030299 NYY @ SD | +110 | -130 | 7.5 | -120 | -102 | `7143a320e381b7bd2f44146f55bdcd1c0ff471a073849cc8f6f56cdd5ec739b9` |
| 36030290 STL @ COL | -120 | +102 | 10.5 | -118 | -104 | `d8dab47bdb1156e40ccce559f0ddf7ae12332629e277e00e6ab491e84eb984f8` |
| 36030293 WSH @ LAD | +180 | -215 | 7 | -110 | -110 | `afd60137a896b5509dae6bd06f2e6912bc1aa265b9e219b4f90f998755bb817b` |
| 36030300 ATH @ SEA | +200 | -245 | 7.5 | -115 | -105 | `4c2697830f280ec6d862347e827d9a8ce9b51b0e2653ff9c56142eeaea01e796` |

The two earlier games (SF–NYM and CHC–MIA) were excluded because they had already started.
