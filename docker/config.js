// Where the backend API is, inside the containers: nginx forwards /api/... to
// the backend (docker/nginx.conf), so pages and API share one address.
window.AIRSENSE_API = window.AIRSENSE_API || "/api";
