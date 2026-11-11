# 每日 AI 真人短劇工廠

每天台灣時間 08:00 由 GitHub Actions 自動寫一個新故事、生成素材、合成直式 60 秒 MP4。正式流程不讀舊的兩部劇本，也不需要每天上傳檔案。

目前採用先前允許的低預算混合形式：**15 秒原生中文對話動態影片（3 × 5 秒），加上 45 秒由 3 張新劇照運鏡的畫面**。動態鏡頭保留原生聲音；劇照鏡頭使用畫外角色配音。這不是整整 60 秒持續真人動態或全片精準對嘴。角色一致性、台詞是否完整及嘴型品質仍需要審片，程式只驗證檔案技術規格。

## 一次性設定

在 [Actions Secrets](https://github.com/as06040806-art/ai-film-factory/settings/secrets/actions) 填入：

| 名稱 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | 自動產生新劇本，帳戶須有 API 額度 |
| `RUNWAYML_API_SECRET` | Runway **Dev API** 的有效金鑰，專案須有預付 credits |

ChatGPT 中的 Runway 連線與 Runway 網頁訂閱，不會自動變成 GitHub 的 Dev API 金鑰。金鑰只存 GitHub Secrets，請勿貼到聊天、程式碼、Issue 或 README。

設定好後，排程會在下一次 08:00 自動執行（GitHub 可能延遲）。要立即製片，在 [Actions](https://github.com/as06040806-art/ai-film-factory/actions/workflows/film.yml) 選 Run workflow → `produce`。`check` 為免費設定檢查，`demo` 為清楚標示的彩色技術測試，不是真人影片。每次推送程式也只做免費測試與檢查。

## 產物在哪裡

Actions → 該次執行 → `daily-film-*` artifact（保留 30 天）：

- `film.mp4`：繁中燒錄字幕版，720 × 1280、H.264/AAC、約 60 秒。
- `film_clean.mp4`：無燒錄字幕版。
- `script.json`、`subtitles.srt`、`subtitles.ass`：故事及可編輯字幕。
- `budget.json`、`cost-report.json`、`render-report.json`：費用預留與技術驗證。
- 失敗時有 `failure.json`／`setup-report.json`，不會把失敗或技術測試說成完成真人影片。

## 費用限制

每次正式製片前，重新讀取 Runway 官方價格、OpenAI 模型價格、臺灣銀行美元即期賣出匯率。查不到、格式變動或估算超過 **NT$50** 時，停止付費生成。Runway Dev 額度也會先檢查。

2026-11-09 驗證的方案是 3 段 480p／5 秒影片（105 credits），1 張 720p 原始劇照（5 credits），2 張有參考圖的劇照（4 credits），合計 **114 Dev credits**。影片正確送出 `ratio: 496:864`，不再用 720p 尺寸冒充 480p 費用。編劇採 `gpt-5.4-mini`，輸入大小及輸出 tokens 有上限。

用美元換算匯率至少 35、15% 稅費緩衝、額外 NT$1 分攤計算，當日預留上限估計約 **NT$48.39**。這是付費前預算保護，不是銀行最終帳單保證。API 預付點數按官方單價計入，失敗或狀態不明的生成也占用當日預留；不會把訂閱內點數視為免費。程式不使用 Runway 網頁訂閱點數。若有額外費用，設定 repository variable `ALLOCATED_COST_TWD`，超過上限就停止。

## 續跑與避免重複扣款

每個台灣日期只有一份生成狀態。付費送出前和取得 task ID 後，立刻寫到獨立 `film-state` 分支。重跑同一天會續接原 task ID，不重新生成；遇到「送出但沒拿到 ID」的網路不確定狀態會停止，需查供應商紀錄才能處理，絕不盲目重送。

保留近期故事摘要並送給編劇避開重複，另外阻擋相同標題、相同台詞指紋與相同結尾。這能降低重複，不保證所有語義相近的笑點都能自動識別。正式流程不使用 `examples/sample-story.json` 或歷史 `stories 2.json`。

此 repo 是公開的；生成劇本與不含秘密的 task ID／狀態會寫入狀態分支。API 金鑰及含簽章的影片網址不寫入 repo。影片 artifacts 依 GitHub 專案權限可存取，沒有自動上傳社群或發布 YouTube。

## 本機檢查

需要 Python 3.11+、FFmpeg、fontconfig、Noto CJK 字型。

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python main.py check --network
python main.py demo --output output/technical-test
```

`demo` 完整渲染 60 秒測試影片，只用測試圖與測試音，不呼叫生成 API。`render --story story.json --assets assets.json` 可使用已有本機素材；每筆 asset 是 `{"kind":"native_video","path":"/absolute/clip.mp4"}` 或 `{"kind":"still","path":"/absolute/still.png"}`，依 12 個鏡頭排列。只有 still 會使用 TTS。這個介面也供有執行環境的 ChatGPT 工作流程使用。

驗證覆蓋費率變動、匯率變動、NT$50 限制、劇本時長與重複、付費請求不確定失敗、已存在任務續跑、原生音訊保留、繁中字幕與完整 MP4 技術規格。正式 API 生成及真人品質不能由 mock 測試取代。

官方介面與價格：[Runway API](https://docs.dev.runwayml.com/api.md)、[Runway 價格](https://docs.dev.runwayml.com/guides/pricing/)、[OpenAI 模型](https://developers.openai.com/api/docs/models/gpt-5.4-mini)、[臺灣銀行](https://rate.bot.com.tw/xrt?Lang=zh-TW)。
