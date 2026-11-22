// Sign-in helpers shared by index.html and signin.html.
// Needs supabase-js loaded first (window.supabase from the CDN script tag).
//
// The Supabase URL and publishable key come from the local server (/api/config),
// which reads them from .env. The publishable key is meant to be public: Row
// Level Security in supabase/schema.sql is what keeps each user's rows private.
// The secret key is never used by this app.

window.PressPassAuth = (() => {
  let clientPromise = null;

  // The Supabase client, created once. Resolves to null if sign-in isn't set up.
  function getClient() {
    if (!clientPromise) {
      clientPromise = fetch("/api/config")
        .then((response) => response.json())
        .then((config) => {
          if (!config.auth_configured) return null;
          return window.supabase.createClient(config.supabase_url, config.supabase_publishable_key, {
            auth: { persistSession: true, autoRefreshToken: true }
          });
        })
        .catch(() => null);
    }
    return clientPromise;
  }

  // Tell PostHog who this is by Supabase user id only (never the email).
  function identify(user) {
    try { if (window.posthog && user) posthog.identify(user.id); } catch (e) { /* analytics must never break the app */ }
  }

  // For pages that need a login: returns the signed-in user, or sends the
  // browser to the sign-in page (and never resolves).
  async function requireUser() {
    const client = await getClient();
    const { data } = client ? await client.auth.getSession() : { data: {} };
    if (!data.session) {
      location.replace("/signin.html");
      return new Promise(() => {});
    }
    identify(data.session.user);
    // Signed out in another tab, or the session expired: go to sign-in.
    client.auth.onAuthStateChange((event, session) => {
      if (!session) location.replace("/signin.html");
    });
    return data.session.user;
  }

  // fetch() for the app's own /api routes, with the sign-in token attached.
  async function apiFetch(url, options = {}) {
    const client = await getClient();
    const { data } = client ? await client.auth.getSession() : { data: {} };
    const headers = { ...(options.headers || {}) };
    if (data.session) headers.Authorization = `Bearer ${data.session.access_token}`;
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) location.replace("/signin.html");
    return response;
  }

  async function signOut() {
    const client = await getClient();
    try { if (window.posthog) posthog.reset(); } catch (e) { /* ignore */ }
    if (client) await client.auth.signOut();
    location.replace("/signin.html");
  }

  return { getClient, identify, requireUser, apiFetch, signOut };
})();
