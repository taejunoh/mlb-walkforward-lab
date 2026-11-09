# Sep 6 public market observation

This is an observation-only capture of the public DraftKings Network page. It
is not a sportsbook admission, normalized market snapshot, or model input.

- URL: `https://dknetwork.draftkings.com/moneyline-total-spread/`
- Request start: `2026-09-06T19:43:25.068940000Z`
- Response complete: `2026-09-06T19:43:25.599064000Z`
- HTTP status: `200`
- Raw response: `raw.html`
- Raw SHA-256: `944cde74c96bab82962772774225a18f33e0ffcfdc6961a7fa997e47218a4c19`
- Provider quote update time: unknown; no provider freshness claim is made.

The response contained these four displayed rows, preserving the page's quote
format (including Unicode minus signs):

| Event | Displayed start | Moneyline | Total |
| --- | --- | --- | --- |
| `34624714` NY Yankees @ SD Padres | 9/6, 04:10PM | SD Padres `−118`; NY Yankees `−102` | 7: Over `−105`; Under `−115` |
| `34624715` Athletics @ SEA Mariners | 9/6, 04:10PM | SEA Mariners `−215`; Athletics `+176` | 7: Over `−117`; Under `−103` |
| `34624716` MIN Twins @ CHI White Sox | 9/6, 06:20PM | CHI White Sox `−143`; MIN Twins `+119` | 8.5: Over `−112`; Under `−108` |
| `34624717` WAS Nationals @ LA Dodgers | 9/6, 10:10PM | LA Dodgers `−199`; WAS Nationals `+163` | 8: Over `−119`; Under `−101` |

The page is a media/public display source, not the fixed sportsbook host
required by the repository's DraftKings admission contract. These rows must
not be passed to `multibook_market_capture` or the DraftKings-only snapshot
admission as if they were direct sportsbook evidence.
