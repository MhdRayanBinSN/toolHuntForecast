const API = import.meta.env.VITE_API_BASE || '';

export async function request(path, options = {}) {
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers };
  let response;
  try { response = await fetch(`${API}${path}`, { ...options, headers }); }
  catch { throw new Error('The research server is not reachable. Start the FastAPI service and try again.'); }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

export const getRuns = () => request('/runs');
export const getRun = (id) => request(`/runs/${id}`);
export const getSchedule = () => request('/schedule');
export const getReports = () => request('/reports');
export const getReport = (id) => request(`/reports/${id}?format=json`);
export const getCategories = () => request('/categories');
export const getCandidates = () => request('/candidates');
export const startRun = (category, mode = 'fast') => request('/runs', { method: 'POST', body: JSON.stringify({ category, mode }) });
export const validateCategory = (body) => request('/categories/validate', { method: 'POST', body: JSON.stringify(body) });
export const createCategory = (body) => request('/categories', { method: 'POST', body: JSON.stringify(body) });
export const updateCategory = (id, body) => request(`/categories/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteCategory = (id) => request(`/categories/${id}`, { method: 'DELETE' });
