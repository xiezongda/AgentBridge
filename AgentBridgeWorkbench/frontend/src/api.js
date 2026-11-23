async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(path, {
    method,
    signal,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.error
    throw new Error(typeof detail === 'string' ? detail : `请求失败 (${response.status})`)
  }
  return payload
}

const item = (path, id) => `${path}/${encodeURIComponent(id)}`
const write = (path, method, body) => request(path, { method, body })
async function list(path, key) {
  const payload = await request(path)
  const records = Array.isArray(payload) ? payload : payload?.[key]
  if (!Array.isArray(records)) throw new Error('服务返回了无效的列表数据。')
  return records
}

export const api = {
  health: () => request('/api/health'),
  environment: (signal) => request('/api/environment', { signal }),
  models: () => list('/api/models', 'models'),
  createModel: (model) => write('/api/models', 'POST', model),
  updateModel: (id, model) => write(item('/api/models', id), 'PUT', model),
  deleteModel: (id) => write(item('/api/models', id), 'DELETE'),
  testModel: (id) => write(`${item('/api/models', id)}/test`, 'POST', {}),
  templates: () => list('/api/templates', 'templates'),
  createTemplate: (template) => write('/api/templates', 'POST', template),
  updateTemplate: (id, template) => write(item('/api/templates', id), 'PUT', template),
  deleteTemplate: (id) => write(item('/api/templates', id), 'DELETE'),
  conversations: () => list('/api/conversations', 'conversations'),
  conversation: (id, signal) => request(item('/api/conversations', id), { signal }),
  createConversation: (data) => write('/api/conversations', 'POST', data),
  updateConversation: (id, data) => write(item('/api/conversations', id), 'PUT', data),
  deleteConversation: (id) => write(item('/api/conversations', id), 'DELETE'),
  updateWorkflow: (id, data) => write(`${item('/api/conversations', id)}/workflow`, 'PUT', data),
  sendMessage: (id, message) => write(`${item('/api/conversations', id)}/messages`, 'POST', { message }),
  restoreWorkflow: (id, turnId) => write(`${item('/api/conversations', id)}/restore-turn-workflow/${encodeURIComponent(turnId)}`, 'POST', {}),
  validate: (workflow, requireExecutable, signal) => request('/api/workflows/validate', {
    method: 'POST', body: { workflow, require_executable: requireExecutable }, signal,
  }),
  previewPrompt: (data, signal) => request('/api/workflows/preview-prompt', { method: 'POST', body: data, signal }),
  exportWorkflow: (data) => write('/api/workflows/export', 'POST', data),
  importWorkflow: (data) => write('/api/workflows/import', 'POST', data),
}
