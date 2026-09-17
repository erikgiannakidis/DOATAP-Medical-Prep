FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot
COPY doatap ./doatap
COPY data ./data
USER bot
ENTRYPOINT ["python", "-m", "doatap"]
CMD ["run"]
