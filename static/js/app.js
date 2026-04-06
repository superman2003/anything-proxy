/* Anything Proxy - Common utilities */

async function apiFetch(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    if (resp.status === 401) {
        window.location.href = '/admin/login';
        return null;
    }
    return resp;
}
