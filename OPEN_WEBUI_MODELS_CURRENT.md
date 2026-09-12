# Open WebUI — Các model hiện tại và vị trí lưu trữ

> Báo cáo kiểm tra cấu hình local: **09/09/2026**  
> Dự án: `D:\ClaudeExperience\Memory\open-webui`  
> **Phạm vi:** snapshot cấu hình máy local, không phải kiến trúc canonical, data map hay release gate. Trạng thái model có thể thay đổi sau thời điểm kiểm tra. Xem `OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md`, `OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md` và `OPEN_WEBUI_RELEASE_GATES.md` cho tài liệu đang được duy trì.

## 1. Kết luận nhanh

Open WebUI hiện được cấu hình để kết nối tới một **Custom OpenAI-compatible Proxy**. Ollama đang tắt.

| Thành phần | Giá trị hiện tại | Trạng thái |
|---|---|---|
| Model chat chính | `custom.gemini-3.6-flash-high` | Đã được ghi nhận sử dụng nhiều nhất |
| Model hình ảnh từng xuất hiện | `custom.gemini-3.1-flash-image` | Đã xuất hiện trong lịch sử chat |
| Model đặc biệt | `arena-model` | Đã xuất hiện 1 lần trong lịch sử |
| Task LLM riêng | Chưa chỉ định | Fallback về model chat được chọn |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` | Được cấu hình cho RAG/Memory |
| Reranker | Không cấu hình | Không sử dụng reranker riêng |
| STT | Whisper `base` mặc định | Chỉ dùng khi chạy speech-to-text |
| TTS | `tts-1`, voice `alloy` | Chỉ dùng khi bật đọc giọng nói |
| Image Generation | Đang tắt | `image_generation.enable = false` |
| Code execution | Pyodide | Đang bật |
| Ollama | Tắt | `ollama.enable = false` |

---

## 2. Model chat chính

### Model đã ghi nhận trong database

Bảng `chat_message` hiện có các model ID sau:

```text
custom.gemini-3.6-flash-high     10 message records
custom.gemini-3.1-flash-image     2 message records
arena-model                       1 message record
```

Model được sử dụng nhiều nhất là:

```text
custom.gemini-3.6-flash-high
```

### Luồng xử lý

```text
Browser/Frontend
    ↓
POST tới Open WebUI Backend
    ↓
Backend thêm system prompt, lịch sử chat, Memory/RAG context
    ↓
Backend gửi OpenAI-compatible request tới Custom Proxy
    ↓
Custom Proxy chuyển tiếp tới model thật
    ↓
Kết quả stream quay lại Backend
    ↓
Backend lưu message và chuyển stream về Frontend
```

Payload khái quát:

```json
{
  "model": "custom.gemini-3.6-flash-high",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "stream": true
}
```

### Vị trí cấu hình

```text
backend/open_webui/config.py
backend/open_webui/routers/openai.py
backend/open_webui/routers/ollama.py
```

Custom Proxy được lưu trong cấu hình database ở nhóm khóa `openai.*`. URL hiện tại là URL OpenAI-compatible của Custom Proxy. API key không được ghi vào tài liệu này.

### Model thật nằm ở đâu?

Các model có prefix `custom.` **không nằm trong thư mục local của Open WebUI**. Đây chỉ là model ID mà Custom Proxy công bố.

Model thật nằm ở hạ tầng phía sau Custom Proxy, ví dụ:

```text
Open WebUI
    ↓ HTTPS
Custom Proxy
    ↓
Gemini/API provider thật
```

Open WebUI chỉ lưu:

- model ID;
- URL Custom Proxy;
- thông tin kết nối;
- lịch sử message và model ID đã dùng.

Open WebUI không lưu trọng số/model files của Gemini local trên máy này.

---

## 3. Task LLM

Task LLM là model được Open WebUI gọi cho các tác vụ phụ, chẳng hạn:

- tạo tiêu đề cuộc hội thoại;
- tạo tag;
- tạo câu hỏi tiếp theo;
- tạo search query;
- tạo image prompt;
- xử lý voice-mode prompt;
- autocomplete;
- một số tác vụ tóm tắt hoặc nén context.

### Cấu hình hiện tại

```text
task.model.default = ""
task.model.external = ""
```

Không có Task LLM riêng được chỉ định. Vì vậy Open WebUI thường fallback về model chat hiện tại, nhiều khả năng là:

```text
custom.gemini-3.6-flash-high
```

Các task đang bật:

```text
task.title.enable = true
task.tags.enable = true
task.follow_up.enable = true
task.query.search.enable = true
task.query.retrieval.enable = true
task.voice.prompt.enable = true
```

Autocomplete đang tắt:

```text
task.autocomplete.enable = false
```

### Vị trí code

```text
backend/open_webui/utils/task.py:16-27
backend/open_webui/routers/tasks.py:141-205
```

Hàm `get_task_model_id()` quyết định model cho task. Endpoint tạo title tạo một payload mới với:

```python
{
    'model': task_model_id,
    'messages': [{'role': 'user', 'content': content}],
    'stream': False,
}
```

Task LLM có thể đi qua cùng Custom Proxy nếu model được chọn là model external.

---

## 4. Embedding model

### Model hiện tại

```text
sentence-transformers/all-MiniLM-L6-v2
```

Cấu hình:

```text
backend/open_webui/config.py:1002
```

```python
RAG_EMBEDDING_MODEL = os.getenv(
    'RAG_EMBEDDING_MODEL',
    'sentence-transformers/all-MiniLM-L6-v2'
)
```

### Vai trò

Embedding model không trả lời như chatbot. Nó biến text thành vector số:

```text
"Open WebUI là gì?"
    ↓
[0.12, -0.44, 0.91, ...]
```

Khi import tài liệu:

```text
File PDF/DOCX/TXT
    ↓
Chia thành chunks
    ↓
Embedding từng chunk
    ↓
Lưu vector vào vector database
```

Khi query:

```text
Query của người dùng
    ↓
Embedding query
    ↓
So sánh với các document vectors
    ↓
Lấy các đoạn liên quan
    ↓
Đưa vào context của model chat chính
```

### Vị trí model files

Model này thường được tải bởi `sentence-transformers`/Hugging Face và được lưu trong cache model của Python/Hugging Face. Vị trí có thể phụ thuộc vào biến môi trường:

```text
HF_HOME
TRANSFORMERS_CACHE
SENTENCE_TRANSFORMERS_HOME
```

Trong môi trường hiện tại, model đã được tải và xác nhận tại:

```text
C:\Users\AnhTris\.cache\huggingface\hub\models--sentence-transformers--all-MiniLM-L6-v2\
```

Snapshot model hiện tại:

```text
C:\Users\AnhTris\.cache\huggingface\hub\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41\
```

Cache này chiếm khoảng 977 MB. Vị trí này có thể thay đổi nếu đặt các biến môi trường `HF_HOME`, `HF_HUB_CACHE`, `TRANSFORMERS_CACHE` hoặc `SENTENCE_TRANSFORMERS_HOME`.

Model này không nằm trong `node_modules` và không nằm trong `build` frontend.

### Vector database

Cấu hình:

```text
VECTOR_DB = chroma
CHROMA_DATA_PATH = f'{DATA_DIR}/vector_db'
```

Vector/chunks của RAG và Memory được lưu tại:

```text
D:\ClaudeExperience\Memory\open-webui\backend\data\vector_db\
```

Code liên quan:

```text
backend/open_webui/retrieval/vector/dbs/chroma.py
backend/open_webui/retrieval/utils.py
```

---

## 5. Reranking model

### Cấu hình hiện tại

```text
rag.reranking_engine = ""
rag.reranking_model = ""
```

Hiện tại không có reranker riêng.

### Vai trò nếu được bật

Embedding search có thể trả về nhiều đoạn gần nghĩa. Reranker sẽ đọc:

```text
Query + từng đoạn tài liệu
```

Sau đó xếp hạng lại đoạn nào phù hợp nhất.

```text
Query
  ↓
Embedding search lấy top-k
  ↓
Reranker chấm điểm lại
  ↓
Chọn các đoạn tốt nhất
  ↓
Gửi vào Primary LLM
```

Reranker thường là cross-encoder, không nhất thiết là LLM sinh văn bản.

Cấu hình nằm tại:

```text
backend/open_webui/config.py:1027-1040
```

---

## 6. Speech-to-Text

### Model mặc định

```text
Whisper model: base
```

Cấu hình:

```text
backend/open_webui/config.py:1547
```

```python
WHISPER_MODEL = os.getenv('WHISPER_MODEL', 'base')
```

### Vai trò

STT chuyển âm thanh thành text:

```text
Microphone/audio file
    ↓
Whisper/STT
    ↓
Text query
    ↓
Primary Chat LLM
```

Đây không phải model chat. Nó chỉ tạo phần text đầu vào.

### Trạng thái hiện tại

```text
audio.stt.engine = ""
audio.stt.model = ""
audio.stt.whisper_model = "base"
```

Do đó Whisper `base` là model mặc định; nó chỉ được load khi bạn sử dụng speech-to-text.

Code liên quan:

```text
backend/open_webui/routers/audio.py
```

---

## 7. Text-to-Speech

### Cấu hình hiện tại

```text
audio.tts.model = "tts-1"
audio.tts.voice = "alloy"
audio.tts.engine = ""
```

### Vai trò

TTS chuyển câu trả lời text thành âm thanh:

```text
Câu trả lời của Primary LLM
    ↓
TTS provider
    ↓
Audio stream/file
    ↓
Frontend phát âm thanh
```

TTS không xử lý query và không tạo câu trả lời. Nó chạy sau khi Primary LLM đã trả lời.

Code liên quan:

```text
backend/open_webui/routers/audio.py
backend/open_webui/routers/openai.py
```

File audio cache, nếu được tạo, nằm dưới:

```text
D:\ClaudeExperience\Memory\open-webui\backend\data\cache\audio\speech\
```

---

## 8. Image Generation

### Cấu hình hiện tại

```text
image_generation.enable = false
image_generation.engine = "openai"
image_generation.model = ""
```

Image Generation hiện đang tắt. Vì vậy model `custom.gemini-3.1-flash-image` từng xuất hiện trong lịch sử chat không chứng minh rằng image generation backend hiện đang bật.

### Luồng nếu được bật

```text
User image prompt
    ↓
Có thể qua Image Prompt Task LLM để tối ưu prompt
    ↓
Image provider/API
    ↓
Ảnh sinh ra
    ↓
Lưu cache/file
    ↓
Frontend hiển thị ảnh
```

Các engine có thể là OpenAI image API, AUTOMATIC1111, ComfyUI hoặc Gemini tùy cấu hình.

Image cache nằm tại:

```text
D:\ClaudeExperience\Memory\open-webui\backend\data\cache\image\generations\
```

Cấu hình và router:

```text
backend/open_webui/config.py:1336-1341
backend/open_webui/routers/images.py
```

---

## 9. Code execution

Code execution không phải model. Đây là môi trường chạy code được LLM sinh ra.

### Cấu hình hiện tại

```text
code_execution.enable = true
code_execution.engine = "pyodide"

code_interpreter.enable = true
code_interpreter.engine = "pyodide"
```

### Luồng hoạt động

```text
User yêu cầu phân tích dữ liệu
    ↓
Primary LLM sinh Python code
    ↓
Pyodide chạy code trong browser
    ↓
Kết quả/bảng/biểu đồ trả về frontend
```

Pyodide files nằm trong frontend assets:

```text
D:\ClaudeExperience\Memory\open-webui\static\pyodide\
D:\ClaudeExperience\Memory\open-webui\build\pyodide\
```

Pyodide không tự là LLM và không thay thế model chat.

---

## 10. Database đang được dùng thực tế

Trong môi trường chạy development hiện tại, `DATA_DIR` được tính từ `BACKEND_DIR / 'data'`. Database đang được sử dụng là:

```text
D:\ClaudeExperience\Memory\open-webui\backend\data\webui.db
```

Các bảng quan trọng:

| Bảng | Nội dung |
|---|---|
| `chat_message` | Message, model ID, output, usage, sources |
| `chat` | Metadata của phiên chat |
| `config` | Cấu hình provider, task, RAG, audio, image, code |
| `memory` | User memory |
| `file` | Metadata file upload |
| `knowledge` | Knowledge Base |
| `model` | Custom model definitions nếu có |

Model ID của các phiên chat được lưu trong cột:

```text
chat_message.model_id
```

Trong database hiện tại, bảng `model` không có custom model row; các model `custom.*` được lấy động từ Custom Proxy.

> Có một file `backend/open_webui/data/webui.db` dung lượng 0 byte trong workspace, nhưng đó không phải database đang chứa dữ liệu của môi trường development hiện tại.

---

## 11. Tổng sơ đồ vị trí

```text
[Model chat chính]
  Không nằm local
  Nằm phía sau Custom Proxy
  ID lưu trong backend/data/webui.db

[Task LLM]
  Thường dùng lại model chat chính
  Cấu hình trong bảng config

[Embedding model]
  File model nằm trong Hugging Face cache
  Vector nằm trong backend/data/vector_db/

[Reranker]
  Hiện chưa cấu hình

[Whisper STT]
  Chỉ load khi dùng audio
  Cache phụ thuộc sentence-transformers/Whisper config

[TTS]
  Gọi provider khi cần
  Audio cache: backend/data/cache/audio/speech/

[Image generation]
  Hiện đang tắt
  Image cache: backend/data/cache/image/generations/

[Pyodide]
  Frontend assets:
  static/pyodide/ và build/pyodide/
```

---

## 12. Cách tự kiểm tra lại

### Kiểm tra model từng được dùng

```powershell
@'
import sqlite3
p = r'D:\ClaudeExperience\Memory\open-webui\backend\data\webui.db'
con = sqlite3.connect(p)
rows = con.execute('''
    SELECT model_id, COUNT(*)
    FROM chat_message
    WHERE model_id IS NOT NULL
    GROUP BY model_id
    ORDER BY COUNT(*) DESC
''').fetchall()
for row in rows:
    print(row)
'@ | & 'D:\ClaudeExperience\Memory\open-webui\.venv\Scripts\python.exe' -
```

### Kiểm tra cấu hình model/RAG/task, không in API key

```powershell
@'
import sqlite3
p = r'D:\ClaudeExperience\Memory\open-webui\backend\data\webui.db'
con = sqlite3.connect(p)
for key, value in con.execute('SELECT key, value FROM config ORDER BY key'):
    if key.startswith(('task.', 'rag.', 'memories.', 'image_generation.', 'code_execution.', 'code_interpreter.')):
        print(key, '=', value)
'@ | & 'D:\ClaudeExperience\Memory\open-webui\.venv\Scripts\python.exe' -
```

Không nên in các khóa dạng `api_key`, `token`, `password` hoặc `secret` ra terminal, log hay file Markdown.
