# WayPointDB

## Overview
WayPointDB is a lightweight GPS data collection API that interacts with a PostgreSQL database and is deployed using Docker and Nginx. WayPointDB is focused on fast and efficient data collection and retrieval, so the user wont have to wait around for the webpage to load or the data to be processed.

## Features
- REST API for GPS data collection
- Pure Flask-based interface
- PostgreSQL as the database backend
- Reverse proxy using Nginx
- Dockerized deployment

## Prerequisites
Before running the project, ensure you have the following installed:
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

## Installation
1. Clone the repository
```bash
git clone https://github.com/yniverz/waypointdb
```

2. Change directory to the project folder
```bash
cd waypointdb
```

3. Build the Docker image
```bash
docker-compose build
```

4. Run the Docker container (```-d``` for detached mode)
```bash
docker-compose up -d
```
