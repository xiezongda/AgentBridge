import { useMemo, useState } from 'react'
import { Clipboard, Download, Upload } from 'lucide-react'
import { api } from './api.js'

function TransferDialog({ title, busy, close, submit, children }) {
  return (
    <div className="dialog-backdrop" onClick={busy ? undefined : close}>
      <form className="dialog transfer-dialog" role="dialog" aria-modal="true" aria-label={title}
        onClick={(event) => event.stopPropagation()} onSubmit={submit}>
        <h2>{title}</h2>
        {children}
      </form>
    </div>
  )
}

export function ExportDialog({ workflow, title, close }) {
  const [name, setName] = useState(title)
  const [description, setDescription] = useState('')
  const [bundle, setBundle] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const json = bundle ? JSON.stringify(bundle, null, 2) : ''

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    try { setBundle(await api.exportWorkflow({ name: name.trim(), description, workflow })) }
    catch (error) { setMessage({ error: true, text: error.message }) }
    finally { setBusy(false) }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `AgentBridge-${bundle.name.replace(/[<>:"/\\|?*]/g, '_').slice(0, 80)}.json`
    document.body.append(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(json)
      setMessage({ text: '已复制 JSON' })
    } catch {
      setMessage({ error: true, text: '浏览器无法写入剪贴板，请手动选中文本或下载 JSON。' })
    }
  }

  return (
    <TransferDialog title="导出流程" busy={busy} close={close} submit={submit}>
      <label>名称<input autoFocus required disabled={busy} value={name} onChange={(event) => { setName(event.target.value); setBundle(null); setMessage(null) }} /></label>
      <label>描述<textarea rows="2" disabled={busy} value={description} onChange={(event) => { setDescription(event.target.value); setBundle(null); setMessage(null) }} /></label>
      <p className="muted">导出时会移除本机目录、可执行文件路径和 API 密钥相关字段。</p>
      {bundle && <label>导出的 JSON<textarea className="tech-input" rows="9" readOnly value={json} /></label>}
      {message && <p role={message.error ? 'alert' : 'status'} className={message.error ? 'error-text' : 'pass-text'}>{message.text}</p>}
      <div className="dialog-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={close}>关闭</button>
        {bundle ? <>
          <button type="button" className="secondary-button" onClick={copy}><Clipboard size={15} />复制 JSON</button>
          <button type="button" className="primary-button" onClick={download}><Download size={15} />下载 JSON</button>
        </> : <button className="primary-button" disabled={busy || !name.trim()}><Download size={15} />{busy ? '生成中' : '生成导出文件'}</button>}
      </div>
    </TransferDialog>
  )
}

function readBundle(json) {
  if (!json.trim()) return {}
  let bundle
  try { bundle = JSON.parse(json.trim()) }
  catch { return { error: '请输入有效的 JSON。' } }
  if (bundle?.format_version !== 2 || !Array.isArray(bundle.workflow?.nodes) || !Array.isArray(bundle.workflow?.edges)) {
    return { error: '请选择 AgentBridge 格式版本 2 的流程文件。' }
  }
  const metadata = new Map(
    (Array.isArray(bundle.model_slots) ? bundle.model_slots : [])
      .filter((slot) => typeof slot?.model_card_id === 'string')
      .map(({ model_card_id, name, model_id }) => [model_card_id, {
        model_card_id, name: typeof name === 'string' ? name : '', model_id: typeof model_id === 'string' ? model_id : '',
      }]),
  )
  const cardIds = new Set(bundle.workflow.nodes.filter((node) => node?.type === 'model')
    .map((node) => node.config?.model_card_id ?? node.model_card_id)
    .filter((id) => typeof id === 'string' && id.trim()))
  return { bundle, slots: [...cardIds].map((id) => metadata.get(id) ?? { model_card_id: id }) }
}

export function ImportDialog({ models, close, apply, refreshTemplates }) {
  const [json, setJson] = useState('')
  const [mapping, setMapping] = useState(new Map())
  const [saveAsTemplate, setSaveAsTemplate] = useState(false)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { bundle, slots, error: parseError } = useMemo(() => readBundle(json), [json])

  function changeJson(value) {
    setJson(value)
    setMapping(new Map())
    setError('')
  }

  async function loadFile(event) {
    const file = event.target.files[0]
    if (!file) return
    setBusy(true)
    setError('')
    try { changeJson(await file.text()) }
    catch { setError('无法读取所选文件。') }
    finally { setBusy(false) }
  }

  async function submit(event) {
    event.preventDefault()
    if (result) {
      apply(result.workflow)
      close()
      return
    }
    if (!bundle) return
    setBusy(true)
    setError('')
    try {
      const imported = await api.importWorkflow({ bundle, card_mapping: Object.fromEntries(mapping), save_as_template: saveAsTemplate })
      setResult(imported)
      if (imported.template) await refreshTemplates()
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return (
    <TransferDialog title="导入流程" busy={busy} close={close} submit={submit}>
      {result ? <>
        {result.template && <p className="pass-text" role="status">已保存模板「{result.template.name}」</p>}
        {result.unmapped_nodes.length > 0 ? (
          <section className="unmapped-nodes" aria-label="未映射节点">
            <p>以下节点需要在画布上选择本地模型卡片。</p>
            <ul>{result.unmapped_nodes.map(({ node_id, model_card_id }) => (
              <li key={node_id}>{result.workflow.nodes.find((node) => node.id === node_id)?.label ?? node_id}{model_card_id ? ` · ${model_card_id}` : ''}</li>
            ))}</ul>
          </section>
        ) : <p className="pass-text" role="status">流程已准备好加载到画布。</p>}
      </> : <>
        <label>选择 JSON 文件<input type="file" accept=".json,application/json" disabled={busy} onChange={loadFile} /></label>
        <label>或粘贴流程 JSON<textarea className="tech-input" rows="7" disabled={busy} value={json} onChange={(event) => changeJson(event.target.value)} /></label>
        {parseError && <p className="error-text" role="alert">{parseError}</p>}
        {slots?.length > 0 && <fieldset className="remapping-fields">
          <legend>模型卡片映射</legend>
          {slots.map(({ model_card_id: oldId, name, model_id }) => {
            const localMatch = models.find((card) => card.id === oldId)
            return (
              <label key={oldId}>
                {name || oldId}{model_id && <small>{model_id}</small>}
                <select disabled={busy} value={mapping.get(oldId) ?? ''} onChange={(event) => {
                  const localId = event.target.value
                  setMapping((current) => {
                    const next = new Map(current)
                    if (localId) next.set(oldId, localId)
                    else next.delete(oldId)
                    return next
                  })
                  setError('')
                }}>
                  <option value="">{localMatch ? `保留本地卡片 · ${localMatch.name}` : '稍后在画布中选择'}</option>
                  {models.map((card) => <option key={card.id} value={card.id}>{card.name} · {card.model_id}</option>)}
                </select>
              </label>
            )
          })}
        </fieldset>}
        {bundle && <label className="check-label"><input type="checkbox" name="save_as_template" disabled={busy} checked={saveAsTemplate} onChange={(event) => setSaveAsTemplate(event.target.checked)} />同时保存为模板</label>}
      </>}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="dialog-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={close}>{result ? '关闭' : '取消'}</button>
        <button className="primary-button" disabled={busy || !bundle}><Upload size={15} />{busy ? '读取中' : result ? '加载到画布' : '导入流程'}</button>
      </div>
    </TransferDialog>
  )
}
