# pdf2md

PDF → Markdown 轉換管線(docling),包成 MCP server 餵給 Claude。
針對 PCIe / CXL spec 這類表格密集的大型規格書設計。

## 環境

- uv 專案,uv-managed CPython 3.12(pin 在 `.python-version`;不要改用 conda 的 python 當 base — pandas 的 C extension 在那種 venv 下會 DLL load failed)
- 預設鎖 PyTorch cu128(NVIDIA Blackwell 世代);其他 GPU/純 CPU 環境請改
  `pyproject.toml` 的 `tool.uv.index` 到對應的 PyTorch wheel index
- 若你的網路有 TLS 攔截(公司 proxy):uv 指令加 `--system-certs`;server 內建
  `truststore` 處理 HuggingFace 模型下載

```powershell
cd <repo 目錄>
uv sync
uv run python -c "import torch; print(torch.cuda.is_available())"   # GPU 環境應為 True
```

## MCP server

註冊(user scope,一次即可):

```powershell
claude mcp add pdf2md --scope user -- uv run --directory <repo 絕對路徑> server.py
```

工具:

| 工具 | 用途 |
|---|---|
| `convert_pdf(pdf_path, pages?, split_by_chapter?, ocr?)` | 轉換 PDF,輸出到 `output\<檔名>\` |
| `list_converted()` | 列出已轉換的文件 |
| `get_conversion_info(name)` | 回傳某文件的章節 index 與檔案清單 |

參數要點:

- `pdf_path`:絕對路徑,或直接放進 `input\` 後只給檔名
- `pages`:`"1-50"` 這種 1-based 頁碼範圍。**上千頁的 spec 請分段轉** — 除了單次呼叫會跑數十分鐘(必要時可設環境變數 `MCP_TOOL_TIMEOUT` 拉長 Claude Code 的工具逾時),圖片抽取模式下 docling 會把整個範圍的頁面點陣圖留在 RAM(2x 約 5 MB/頁,1000 頁 ≈ 5 GB+)
- `split_by_chapter=True`:依章節切成一章一檔 + `_index.md`,spec 建議開啟(單檔太大 Claude 不好讀)
- `ocr`:預設關(數位文字 PDF 快很多),掃描檔才開

## 圖片處理

- 每張圖抽成 PNG(2x = 144 DPI)存到 `output\<檔名>\artifacts<範圍>\`,markdown 內以相對路徑引用(單檔 md 用 `artifacts…/x.png`,章節檔用 `../artifacts…/x.png`)
- Claude 讀 markdown 看到 `![Image](…)` 時,直接 Read 那個 PNG 就能看圖
- 頁首 logo、「Evaluation Copy」浮水印這類雜圖也會被抽出(docling 不分辨),屬預期行為
- 同一頁碼範圍重轉會先清掉舊的 artifacts 目錄,不會累積殘檔

## 注意

- **首次轉換會從 HuggingFace 下載 docling 模型(數百 MB)**,只慢這一次
- 轉出的 markdown 放在 `output\`,filesystem MCP / Claude 可直接讀
- 已知限制:docling 偶爾把帶編號的標題輸出成無編號(如 1.5 "Flex Bus Link Features"),`split_by_chapter` 會把該節併入前一章
