FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码与页面/静态资源
COPY . .

EXPOSE 8642

# 单进程 WSGI（waitress）：会话/任务在内存中，见 serve.py 顶部说明
CMD ["python", "serve.py"]
