import { useCallback, useEffect, useState } from 'react'
import { llmApi, type LLMConfig, type LLMTestResult } from '../api/llm'
import './LLMSettings.css'

/**
 * LLM provider 手动切换面板。
 *
 * 三条如实告知（都是后端真实语义，界面不能含糊）：
 *  1. **只对当前后端进程生效**，重启后回到环境变量/compose 的配置；
 *  2. API Key **只存后端内存、不落盘**，读取时脱敏显示；
 *  3. 只能在后端已注册的 provider 里选 —— 不给任意 base_url，避免把服务端
 *     变成向任意地址发请求的通道。
 */
export function LLMSettings({ onClose, onChanged }: {
  onClose: () => void
  onChanged?: (cfg: LLMConfig) => void
}) {
  const [cfg, setCfg] = useState<LLMConfig | null>(null)
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<LLMTestResult | null>(null)
  const [banner, setBanner] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const c = await llmApi.config()
      setCfg(c)
      setProvider(c.provider)
      setModel(c.model)
      setBaseUrl(c.base_url)
      onChanged?.(c)
    } catch (e) {
      setBanner({ kind: 'err', text: String((e as Error).message) })
    }
  }, [onChanged])

  useEffect(() => { load() }, [load])

  const pick = (name: string) => {
    setProvider(name)
    setTest(null)
    const p = cfg?.providers?.[name]
    if (p) {
      // 切 provider 时把模型/base_url 换成它的默认值 ——
      // 否则会把 A 家的模型名带到 B 家的端点上（后端也会拒绝这种混搭）
      setModel(p.default_model)
      setBaseUrl(p.base_url)
    }
  }

  const body = () => ({
    provider, model, base_url: baseUrl || undefined,
    api_key: apiKey || undefined,
  })

  const onTest = async () => {
    setBusy(true); setTest(null); setBanner(null)
    try {
      const r = await llmApi.test(body())
      setTest(r)
    } catch (e) {
      setBanner({ kind: 'err', text: String((e as Error).message) })
    } finally { setBusy(false) }
  }

  const onSave = async () => {
    setBusy(true); setBanner(null)
    try {
      const r = await llmApi.switchTo(body())
      if (r.ok) {
        setBanner({ kind: 'ok', text: `已切换到 ${r.provider} / ${r.model}。${r.note || ''}` })
        setApiKey('')
        await load()
      } else {
        setBanner({ kind: 'warn', text: r.error || '切换失败' })
        await load()
      }
    } catch (e) {
      const err = e as Error & { body?: { error?: string } }
      setBanner({ kind: 'err', text: err.body?.error || err.message })
    } finally { setBusy(false) }
  }

  const onReset = async () => {
    setBusy(true); setBanner(null)
    try {
      const r = await llmApi.reset()
      setBanner({ kind: 'ok', text: `已回到环境变量配置：${r.provider} / ${r.model}` })
      setApiKey('')
      await load()
    } catch (e) {
      setBanner({ kind: 'err', text: String((e as Error).message) })
    } finally { setBusy(false) }
  }

  const cur = cfg?.providers?.[provider]

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal llm-modal" onClick={e => e.stopPropagation()}>
        <h3>🧠 模型设置（LLM provider）</h3>

        {banner && (
          <div className={`llm-banner ${banner.kind}`}>
            {banner.text}
            <button className="link" onClick={() => setBanner(null)}>关闭</button>
          </div>
        )}

        <div className="llm-current">
          <div className="llm-row">
            <span className="llm-k">当前生效</span>
            <span className="llm-v">
              <code>{cfg?.provider || '…'}</code> / <code>{cfg?.model || '…'}</code>
            </span>
            {cfg?.runtime.active
              ? <span className="llm-tag warn">手动覆盖中</span>
              : <span className="llm-tag">环境变量</span>}
          </div>
          <div className="llm-row">
            <span className="llm-k">Base URL</span>
            <span className="llm-v mono">{cfg?.base_url || '…'}</span>
          </div>
          <div className="llm-row">
            <span className="llm-k">API Key</span>
            <span className="llm-v mono">{cfg?.api_key || '(none)'}</span>
          </div>
        </div>

        <div className="llm-section-title">选择 provider</div>
        <div className="llm-providers">
          {Object.entries(cfg?.providers || {}).map(([name, p]) => (
            <button
              key={name}
              className={`llm-provider${provider === name ? ' on' : ''}`}
              onClick={() => pick(name)}
            >
              <span className="lp-name">{name}</span>
              <span className="lp-model">{p.default_model}</span>
              <span className={`lp-key ${p.key_present ? 'ok' : 'miss'}`}>
                {p.key_present ? `✅ ${p.key_masked}` : '❌ 无 key'}
              </span>
            </button>
          ))}
        </div>

        <div className="llm-form">
          <label>
            <span>模型</span>
            <input value={model} onChange={e => setModel(e.target.value)}
                   placeholder={cur?.default_model || '模型名'} />
          </label>
          <label>
            <span>API Key</span>
            <input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
                   placeholder={cur?.key_present ? '留空则用环境变量里的 key' : '该 provider 无 key，需在此填入'} />
          </label>
          <button className="link llm-adv" onClick={() => setAdvanced(v => !v)}>
            {advanced ? '收起高级设置' : '高级设置（Base URL）'}
          </button>
          {advanced && (
            <label>
              <span>Base URL</span>
              <input value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
            </label>
          )}
        </div>

        {test && (
          <div className={`llm-test ${test.ok ? 'ok' : 'err'}`}>
            <div className="lt-head">
              {test.ok ? '✅ 连接成功' : '❌ 连接失败'}
              <span className="lt-meta">
                {test.provider} / {test.model || test.requested_model}
                {typeof test.latency_ms === 'number' ? ` · ${test.latency_ms}ms` : ''}
                {test.tokens ? ` · ${test.tokens} tokens` : ''}
              </span>
            </div>
            {test.model_mismatch && (
              <div className="lt-warn">
                ⚠ 实际使用的模型（{test.model}）与请求的（{test.requested_model}）不一致
              </div>
            )}
            {test.sample && <div className="lt-sample">返回：{test.sample}</div>}
            {test.error && <div className="lt-err">{test.error}</div>}
          </div>
        )}

        <div className="llm-note">
          · 切换<strong>只对当前后端进程生效</strong>，重启后端会回到环境变量/compose 的配置；<br />
          · 填入的 API Key <strong>只存在后端内存、不落盘</strong>；<br />
          · 只能选后端已注册的 provider（不提供任意 URL，避免服务端被当成请求转发通道）；<br />
          · 「测试连接」<strong>不会保存配置</strong>，且只试当前选中的 provider（不会用容灾链兜住失败）。
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>关闭</button>
          <button onClick={onReset} disabled={busy || !cfg?.runtime.active}
                  title={cfg?.runtime.active ? '回到环境变量配置' : '当前没有手动覆盖'}>
            恢复默认
          </button>
          <button onClick={onTest} disabled={busy}>🧪 测试连接</button>
          <button className="primary" onClick={onSave} disabled={busy}>
            {busy ? '处理中…' : '保存并切换'}
          </button>
        </div>
      </div>
    </div>
  )
}
