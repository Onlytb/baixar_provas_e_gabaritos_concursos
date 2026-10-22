FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ACERVO_DIR=/data STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY acervo ./acervo
COPY app.py coletor_provas_pci.py entrypoint.py ./
COPY tests ./tests
EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health',timeout=3)"
CMD ["python", "entrypoint.py"]
