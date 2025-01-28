# Use official Python image
FROM python:3.10

# Set working directory
WORKDIR /app

# Copy project files
COPY requirements.txt requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . .

# Expose Flask port
EXPOSE 8500

# Run Waitress
CMD ["python", "-u", "app.py"]