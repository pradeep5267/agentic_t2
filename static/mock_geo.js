// static/mock_geo.js
(function() {
  // Single test point (just inside Bourne End)
  const lat = 51.5799463, lon = -0.7120777;

  // Override geolocation
  navigator.geolocation = navigator.geolocation || {};
  navigator.geolocation.watchPosition = function(success, error, opts) {
    // Immediately invoke success once
    setTimeout(() => {
      success({
        coords: {
          latitude:  lat,
          longitude: lon,
          accuracy:  5
        },
        timestamp: Date.now()
      });
    }, 0);
    // Return a dummy watch ID
    return 1;
  };
  navigator.geolocation.clearWatch = function(id) { /* no-op */ };

  console.log("📍 mock_geo.js loaded: overriding watchPosition to", lat, lon);
})();
