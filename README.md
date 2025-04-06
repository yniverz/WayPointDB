[![License: NCPUL](https://img.shields.io/badge/license-NCPUL-blue.svg)](./LICENSE.md)

# WayPointDB

## Overview
WayPointDB is a lightweight GPS data collection API, just like Google's Timeline feature, that interacts with a PostgreSQL database and is deployed using Docker and Nginx. 
WayPointDB is focused on fast and efficient data collection and retrieval, so the user wont have to wait around for the webpage to load or the data to be processed, and can easily sift through their data using the Stats and Map pages.

## Key Benefits
- Multi-User support
- Multi-Trace per User support
- REST API for GPS data collection
- Pure Flask-Jinja-based interface
    - Server side rendering for fast page loads
- Dockerized deployment

<i>Map page</i>
![Screenshot of the Map page](https://github.com/user-attachments/assets/ed178066-c75d-4471-b9c5-b675c6dd33a8)

<i>Stats page</i>
![Screenshot of the Stats page](https://github.com/user-attachments/assets/c11e8beb-a8e8-4a07-bb2a-c95e5020b047)

<i>Import page</i>
![Screenshot of the Import page](https://github.com/user-attachments/assets/37782b04-b6fe-4f08-a516-d3303f25b22b)

<i>Lifetime Heatmap</i>
![Screenshot of the Lifetime Heatmap](https://github.com/user-attachments/assets/4feba90f-c3ed-474f-979d-87a819b694b7)


## Prerequisites
To run this project, you could use the following tools:
- [Docker](https://www.docker.com/)
- [Docker Compose Standalone](https://docs.docker.com/compose/install/standalone/)

## Installation
To install the project, clone the repository, change directory to the project folder, build the Docker image, and run the Docker container (```-d``` for detached mode)
```bash
git clone https://github.com/yniverz/WayPointDB
cd WayPointDB
docker-compose build
docker-compose up -d
```
**Note:** Using Portainer to deploy this repo might not work. in that case just use the above manual deployment.

## Configuration
The project can be configured by modifying variables in the ```docker-compose.yml``` file. The following variables are commonly modified:
- <strong>backend: environment: ```PHOTON_SERVER_*```</strong>
The host, HTTPS status, and an optional API key of the Photon server
- <strong>nginx: ports: ```80:80```</strong>
The first port is the port at which WayPointDB is accessible, and can be customized to an available port on the host machine.

## Usage

### First Steps
The project can be accessed by navigating to the IP address of the host machine on the specified port. Log in with the default credentials: ```admin@example.com``` and ```password```. The E-Mail and password can be changed in the ```Account``` page and is strongly recommended.

### Data Collection
There are currently two ways to collect GPS data using a mobile device:
- Using the Overland app, which can be configured to send data to WayPointDB using the ```/api/v1/gps/overland?api_key=<KEY>``` API endpoint
- Using the OwnTracks app, which can be configured to send data to WayPointDB using the ```/api/v1/gps/owntracks?api_key=<KEY>``` API endpoint
- Using the [WayPointDB iOS App](https://github.com/yniverz/WayPointDB-iOS) which however has to be built and installed yourself as it is not available on the App Store
- **Apple AirTags** are also finally supported using the [WayPointDB AirTag Integration](https://github.com/yniverz/WayPointDB-AirTag-Integration)

#### Alternative Endpoints
- ```/api/v1/gps/overland``` can also be reached using ```/api/v1/overland/batches```
- ```/api/v1/gps/owntracks``` can also be reached using ```/api/v1/owntracks/points```

### Imports
GPS data can be imported from a json file using the ```Import``` page, accessible from the ```Account``` page. The json file should be in a specific format, and an example can be found on the import page.

#### Common Formats
You can import GPS data from various sources, such as **Google Timeline**, **GPX** and **GeoJson** files. In order to do so, you need to transform the data to the required format. WayPointDB provides a transformation tool that can be accessed by visiting the ```/static/transform``` page and selecting the appropriate format.
First select an available format, then select the file to be transformed. After downloading the transformed file, you can import it using the ```Import``` page.

### API
An API key for each user can be generated in the respective ```Account``` page, and is required for API requests.

WayPointDB has a Swagger API documentation page that can be accessed by navigating to ```/api/v1/docs```.

### Users
Users can be added, edited, and deleted in the ```Manage Users``` page. Only ```admin``` users can view this page.

### Additional Traces
Users can add additional traces to their account in the ```Manage Traces``` page accessible from the ```Account``` page. These traces are like profiles for each account and can be selected in the menu bar. Traces can be shared or transfered to other users as well.

## Update
To update the project, pull the latest changes from the repository and rebuild the Docker image
```bash
docker-compose down
git pull
docker-compose build
docker-compose up -d
```
Alternatively these steps can be automated by running ```./update.sh```. To prepare the script for execution, run ```chmod +x update.sh``` first.


## Photon Server
WayPointDB can use a [Photon server](https://github.com/komoot/photon) for reverse geocoding. It is recommended to use a self-hosted instance of the Photon server to avoid rate limiting, and to ensure the privacy of the data. Some hosting providers use an api key for authentication, which can be set in the ```docker-compose.yml``` file. WayPointDB will pass this as the ```X-Api-Key``` header in the requests to the Photon server.

To Use Photon you need to set the following two to three environment variables for the `backend` in the ```docker-compose.yml``` file:
```yaml
services:
  ...
  backend:
    ...
    environment:
      ...
      - PHOTON_SERVER_HOST=photon.domain.tld(:port)   # e.g. photon.komoot.io
      - PHOTON_SERVER_HTTPS=false                     # true if the server uses HTTPS
      - PHOTON_SERVER_API_KEY=                        # optional, only if the server requires an api key
```

<hr>

### References
- Highly inspired by [DaWarIch](https://github.com/Freika/dawarich) but with a focus on "as fast as possible" web UI, data viewing and retrieval
- Icon made by [Freepik](https://www.flaticon.com/authors/freepik)
