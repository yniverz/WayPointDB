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
To install the project, clone the repository, change directory to the project folder, build the Docker image, and run the Docker container (```-d``` for detached mode)
```bash
git clone https://github.com/yniverz/WayPointDB
cd WayPointDB
docker-compose build
docker-compose up -d
```

### Update
To update the project, pull the latest changes from the repository and rebuild the Docker image
```bash
docker-compose down
git pull
docker-compose build
docker-compose up -d
```