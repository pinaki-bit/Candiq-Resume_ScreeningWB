FROM python:3.12-slim

# Create a non-root user (Hugging Face Spaces requirement)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Install dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the spaCy model
RUN python -m spacy download en_core_web_lg

# Copy application code
COPY --chown=user . .

# Create uploads directory with correct permissions
RUN mkdir -p uploads

# Hugging Face Spaces uses port 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
