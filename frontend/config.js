// Backend (Planner agent, the app in ../backend) base URL - picked automatically:
//   UI opened on this machine (localhost / 127.0.0.1)  ->  local backend
//   UI opened anywhere else (Cloud Foundry, BAS, ...)   ->  deployed backend
// A ?api=https://<host> in the browser URL still overrides both (see app.js).
(() => {
  const LOCAL_BACKEND = "http://localhost:8000";
  const DEPLOYED_BACKEND = "https://smart-material-chat-s43-insightful-rabbit-fq.cfapps.us10-001.hana.ondemand.com";

  const isLocal = ["localhost", "127.0.0.1", "[::1]", ""].includes(location.hostname);
  window.APP_CONFIG = { apiBase: isLocal ? LOCAL_BACKEND : DEPLOYED_BACKEND };
})();
