FROM python:3.12-slim
WORKDIR /project
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data/requirements.json data/vendor_master.csv data/document_profiles.csv ./data/
COPY .streamlit ./.streamlit
EXPOSE 8501
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
