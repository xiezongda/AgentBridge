import { useEffect, useState } from 'react'
import { ArrowLeft, Check, FlaskConical, Plus, Save, Sparkles, Trash2 } from 'lucide-react'
import { api } from './api.js'

const defaults = {
  name: '', provider: 'codex_cli', model_id: '', executable: '', effort: 'high', timeout_s: 600,
  sandbox_mode: 'read-only', workdir: '', base_url: '', api_key_env: '', api_key: '',
  has_api_key: false, temperature: '', max_tokens: '',
}
const providers = { codex_cli: 'Codex CLI', agy_cli: 'Antigravity CLI', openai_compat: 'OpenAI 兼容 API', ollama: 'Ollama' }
const isCliProvider = (provider) => provider === 'agy_cli' || provider === 'codex_cli'
const effortOptions = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
const sandboxOptions = ['read-only', 'workspace-write', 'danger-full-access']

function EnvironmentStatus() {
  const [environment, setEnvironment] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api.environment(controller.signal).then(setEnvironment).catch((err) => {
      if (!controller.signal.aborted) setError(err.message)
    })
    return () => controller.abort()
  }, [])

  return (
    <section className="environment-status" aria-label="运行环境">
      <h2>运行环境</h2>
      {error ? <p className="error-text" role="status">环境检测失败 · {error}</p> : environment ? (
        <>
          <p className="muted">{environment.platform} · Python {environment.python_version}</p>
          <ul className="environment-cli-list">
            {Object.entries(environment.detected_clis).map(([provider, cli]) => (
              <li key={provider}>
                <span>{providers[provider] ?? provider}</span>
                <span className={cli.available ? 'pass-text' : 'muted'}>{cli.available ? '已检测到' : 'PATH 中未找到'}</span>
                <code title={cli.command}>{cli.command}</code>
              </li>
            ))}
          </ul>
          <p className="muted">支持 {environment.supported_providers.map((provider) => providers[provider] ?? provider).join('、')}</p>
        </>
      ) : <p className="muted" role="status">正在检测 CLI…</p>}
    </section>
  )
}

export default function ModelLibrary({ models, refresh, onBack }) {
  const [id, setId] = useState(models[0]?.id ?? '')
  const [draft, setDraft] = useState({ ...defaults, ...models[0], api_key: '' })
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState(null)
  const isCli = isCliProvider(draft.provider)

  function select(card) {
    setId(card?.id ?? '')
    setDraft({ ...defaults, ...card, api_key: '' })
    setMessage(null)
  }

  function change(field, value) {
    setDraft((current) => ({ ...current, [field]: value }))
    setMessage(null)
  }

  async function save(event) {
    event.preventDefault()
    setBusy('save')
    setMessage(null)
    try {
      const data = {
        name: draft.name.trim(), provider: draft.provider, model_id: draft.model_id.trim(),
        timeout_s: Number(draft.timeout_s),
      }
      if (isCli) {
        Object.assign(data, {
          executable: draft.executable?.trim() || null, effort: draft.effort,
          sandbox_mode: draft.sandbox_mode, workdir: draft.workdir?.trim() || null,
        })
      } else {
        Object.assign(data, {
          executable: null, base_url: draft.base_url?.trim(), api_key_env: draft.api_key_env?.trim() || null,
          temperature: draft.temperature == null || draft.temperature === '' ? null : Number(draft.temperature),
          max_tokens: draft.max_tokens == null || draft.max_tokens === '' ? null : Number(draft.max_tokens),
        })
        if (draft.api_key) data.api_key = draft.api_key
      }
      const card = id ? await api.updateModel(id, data) : await api.createModel(data)
      await refresh()
      select(card)
      setMessage({ ok: true, text: '模型卡片已保存' })
    } catch (error) { setMessage({ ok: false, text: error.message }) }
    finally { setBusy('') }
  }

  async function remove() {
    if (!window.confirm(`删除模型卡片“${draft.name}”？`)) return
    setBusy('delete')
    setMessage(null)
    try {
      await api.deleteModel(id)
      const cards = await refresh()
      select(cards[0])
    } catch (error) { setMessage({ ok: false, text: error.message }) }
    finally { setBusy('') }
  }

  async function test() {
    setBusy('test')
    setMessage(null)
    try {
      const result = await api.testModel(id)
      setMessage({ ok: result.ok, text: result.ok ? `${result.message || '连接成功'} · ${result.latency_ms} ms` : result.error || result.message || '连接失败' })
    } catch (error) { setMessage({ ok: false, text: error.message }) }
    finally { setBusy('') }
  }

  return (
    <section className="library-page">
      <header className="page-header">
        <button className="icon-button" title="返回" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="page-title"><h1>模型库</h1><small>{models.length} 张模型卡片</small></div>
        <button className="primary-button" disabled={Boolean(busy)} onClick={() => select()}><Plus size={17} />添加模型</button>
      </header>
      <div className="library-layout">
        <aside className="library-list" aria-label="模型卡片">
          {models.map((card) => (
            <button key={card.id} className={`library-card ${id === card.id ? 'active' : ''}`} onClick={() => select(card)} disabled={Boolean(busy)}>
              <span className={`provider-icon ${card.provider}`}><Sparkles size={18} /></span>
              <span><strong>{card.name}</strong><small>{card.model_id}</small></span>
            </button>
          ))}
        </aside>
        <form className="library-form" onSubmit={save}>
          <div className="library-form-title"><span className={`provider-icon ${draft.provider}`}><Sparkles size={21} /></span><h2>{id ? draft.name : '新模型卡片'}</h2></div>
          <div className="form-pair">
            <label>显示名称<input required value={draft.name} onChange={(event) => change('name', event.target.value)} /></label>
            <label>提供方<select value={draft.provider} onChange={(event) => {
              const provider = event.target.value
              setDraft((current) => ({ ...current, provider, executable: '', api_key: '' }))
              setMessage(null)
            }}>{Object.entries(providers).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </div>
          <label>模型 ID<input required className="tech-input" value={draft.model_id} onChange={(event) => change('model_id', event.target.value)} /></label>
          {isCli ? <>
            <label>CLI 可执行文件<input className="tech-input" value={draft.executable ?? ''} onChange={(event) => change('executable', event.target.value)} placeholder="留空自动从 PATH 查找" /></label>
            <label>工作目录<input className="tech-input" value={draft.workdir ?? ''} onChange={(event) => change('workdir', event.target.value)} placeholder="默认工作目录" /></label>
          </> : <>
            <label>API 基础地址<input required type="url" name="base_url" className="tech-input" value={draft.base_url ?? ''} onChange={(event) => change('base_url', event.target.value)} placeholder={draft.provider === 'ollama' ? 'http://127.0.0.1:11434' : 'https://api.openai.com/v1'} /></label>
            <div className="form-pair">
              <label>API 密钥环境变量<input name="api_key_env" className="tech-input" value={draft.api_key_env ?? ''} onChange={(event) => change('api_key_env', event.target.value)} placeholder="例如 OPENAI_API_KEY" /></label>
              <label>API 密钥<input type="password" name="api_key" autoComplete="new-password" value={draft.api_key} onChange={(event) => change('api_key', event.target.value)} placeholder={draft.has_api_key ? '留空保留已有密钥' : '可选'} />{draft.has_api_key && <small className="pass-text">已保存本地密钥</small>}</label>
            </div>
          </>}
          <div className="form-pair form-triple">
            <label>超时 (s)<input required type="number" min="1" step="1" value={draft.timeout_s} onChange={(event) => change('timeout_s', event.target.value)} /></label>
            {isCli ? <>
              <label>推理强度<select value={draft.effort} onChange={(event) => change('effort', event.target.value)}>{effortOptions.map((effort) => <option key={effort}>{effort}</option>)}</select></label>
              <label>沙箱模式<select value={draft.sandbox_mode} onChange={(event) => change('sandbox_mode', event.target.value)}>{sandboxOptions.map((mode) => <option key={mode}>{mode}</option>)}</select></label>
            </> : <>
              <label>采样温度<input name="temperature" type="number" min="0" step="any" value={draft.temperature ?? ''} onChange={(event) => change('temperature', event.target.value)} placeholder="提供方默认值" /></label>
              <label>最大生成 token<input name="max_tokens" type="number" min="1" step="1" value={draft.max_tokens ?? ''} onChange={(event) => change('max_tokens', event.target.value)} placeholder="提供方默认值" /></label>
            </>}
          </div>
          {message && <p role="status" className={`message-inline ${message.ok ? 'pass-text' : 'error-text'}`}>{message.ok && <Check size={15} />}{message.text}</p>}
          <div className="library-actions">
            {id && <><button className="icon-button danger" type="button" disabled={Boolean(busy)} onClick={remove} title="删除模型"><Trash2 size={17} /></button><button className="secondary-button" type="button" disabled={Boolean(busy)} onClick={test}><FlaskConical size={16} />{busy === 'test' ? '测试中' : '测试连接'}</button></>}
            <button className="primary-button" disabled={Boolean(busy) || !draft.name.trim() || !draft.model_id.trim()}><Save size={16} />{busy === 'save' ? '保存中' : '保存卡片'}</button>
          </div>
          <EnvironmentStatus />
        </form>
      </div>
    </section>
  )
}
