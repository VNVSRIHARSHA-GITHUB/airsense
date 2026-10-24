// Where the backend API is. Local development: the backend on port 8000.
// In the Stage 8 containers nginx forwards /api to the backend, and this becomes "/api".
window.AIRSENSE_API = window.AIRSENSE_API || "http://localhost:8000";
