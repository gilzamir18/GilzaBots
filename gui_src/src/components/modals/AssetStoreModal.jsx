import React, { useCallback, useEffect, useState } from 'react';
import { X, Store, Package, FileText, Download, Check, Plus, RefreshCw, RotateCcw, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

// Asset icon with a graceful fallback to a generic icon per asset kind.
function AssetIcon({ src, alt, fallback: Fallback = Package }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <div className="skill-card-icon skill-card-icon-fallback">
        <Fallback size={28} />
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      className="skill-card-icon"
      onError={() => setFailed(true)}
    />
  );
}

// Tab button shared by the asset-type tabs and the skills sub-tabs.
function TabButton({ active, onClick, children }) {
  return (
    <button className={`vscode-modal-tab${active ? ' active' : ''}`} onClick={onClick}>
      {children}
    </button>
  );
}

// "Asset Store" window: one tab per asset type. Skills browse the installable
// AssetStore catalog and manage which runtime skills (SKILL.md) are active for
// the current project; templates are LaTeX packages unpacked at the project root.
export default function AssetStoreModal({
  onClose, projectPath, onWorkspaceChanged,
}) {
  const { t } = useTranslation();
  const [assetType, setAssetType] = useState('skill');
  const [skillsTab, setSkillsTab] = useState('catalog');
  const [assets, setAssets] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [skills, setSkills] = useState([]);
  const [installingId, setInstallingId] = useState(null);
  const [togglingName, setTogglingName] = useState(null);
  const [refreshingName, setRefreshingName] = useState(null);
  const [refreshError, setRefreshError] = useState(null);
  // What the last install actually wrote. The install itself takes milliseconds,
  // so without a message that outlives it the user only sees the button flicker.
  const [installMessage, setInstallMessage] = useState(null);
  // Set when an install would overwrite files already in the project: the user
  // sees what would be replaced before the overwrite is sent.
  const [pendingOverwrite, setPendingOverwrite] = useState(null);

  const fetchAssets = useCallback(() => {
    return fetch('/api/assets?type=skill')
      .then(r => r.ok ? r.json() : { assets: [] })
      .then(data => setAssets(Array.isArray(data.assets) ? data.assets : []))
      .catch(() => setAssets([]));
  }, []);

  const fetchTemplates = useCallback(() => {
    const q = projectPath ? `&projectPath=${encodeURIComponent(projectPath)}` : '';
    return fetch(`/api/assets?type=template${q}`)
      .then(r => r.ok ? r.json() : { assets: [] })
      .then(data => setTemplates(Array.isArray(data.assets) ? data.assets : []))
      .catch(() => setTemplates([]));
  }, [projectPath]);

  const fetchSkills = useCallback(() => {
    if (!projectPath) { setSkills([]); return Promise.resolve(); }
    return fetch(`/api/skills?projectPath=${encodeURIComponent(projectPath)}`)
      .then(r => r.ok ? r.json() : { skills: [] })
      .then(data => setSkills(Array.isArray(data.skills) ? data.skills : []))
      .catch(() => setSkills([]));
  }, [projectPath]);

  useEffect(() => { fetchAssets(); }, [fetchAssets]);
  useEffect(() => { fetchTemplates(); }, [fetchTemplates]);
  useEffect(() => { fetchSkills(); }, [fetchSkills]);

  // A message about templates makes no sense while looking at skills.
  useEffect(() => { setInstallMessage(null); setRefreshError(null); }, [assetType]);

  // The catalog's "Installed" state is about the local copy on disk, not about
  // whether the skill is currently activated for the project.
  const installedNames = new Set(skills.filter(s => s.installedLocally).map(s => s.name));

  const handleInstall = async (asset) => {
    if (!projectPath || installingId) return;
    setInstallingId(asset.id);
    setRefreshError(null);
    setInstallMessage(null);
    try {
      const res = await fetch('/api/assets/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: asset.id, type: asset.type || 'skill', projectPath }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok) {
        // Stay busy until the project view has caught up: the button coming back
        // is what tells the user the files are actually there.
        await fetchSkills();
        await onWorkspaceChanged?.();
        setInstallMessage(t('assetStore.installedSkillMessage', 'Skill "{{name}}" installed in this project.', { name: asset.name }));
      } else {
        setRefreshError(payload.error || t('assetStore.installSkillFailed', 'Could not install the skill.'));
      }
    } catch (err) {
      setRefreshError(err.message);
    } finally {
      setInstallingId(null);
    }
  };

  // Templates unpack at the project root, so an install can replace files the
  // user already has. The first attempt never overwrites: a 409 comes back with
  // the conflicting paths and the user confirms before the second attempt.
  const handleInstallTemplate = async (template, overwrite = false) => {
    if (!projectPath || installingId) return;
    setInstallingId(template.id);
    setRefreshError(null);
    setInstallMessage(null);
    try {
      const res = await fetch('/api/assets/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: template.id, type: 'template', projectPath, overwrite }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok) {
        setPendingOverwrite(null);
        await fetchTemplates();
        await onWorkspaceChanged?.();
        const written = typeof payload.files === 'number' ? payload.files : null;
        setInstallMessage(written === null
          ? t('assetStore.installedTemplateMessage', 'Template "{{name}}" unpacked at the project root.', { name: template.name })
          : t('assetStore.installedTemplateFilesMessage', 'Template "{{name}}" unpacked at the project root ({{count}} files).', { name: template.name, count: written }));
      } else if (res.status === 409) {
        setPendingOverwrite({ id: template.id, conflicts: payload.conflicts || [] });
      } else {
        setRefreshError(payload.error || t('assetStore.installFailed', 'Could not install the template.'));
      }
    } catch (err) {
      setRefreshError(err.message);
    } finally {
      setInstallingId(null);
    }
  };

  // Refresh a project-local copy from the catalog, or drop it so the bundled
  // skill it shadows takes over again. Kept as two explicit endpoints: one
  // reinstalls, the other deletes, and the button says which one it is.
  const handleRefreshLocal = async (skill, endpoint) => {
    if (!projectPath || refreshingName) return;
    setRefreshingName(skill.name);
    setRefreshError(null);
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectPath, name: skill.name }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok) {
        await fetchSkills();
        await onWorkspaceChanged?.();
      } else {
        setRefreshError(payload.error || t('assetStore.refreshFailed', 'Could not refresh the local copy.'));
      }
    } catch (err) {
      setRefreshError(err.message);
    } finally {
      setRefreshingName(null);
    }
  };

  const handleToggleActive = async (skill) => {
    if (!projectPath || skill.mandatory || togglingName) return;
    setTogglingName(skill.name);
    const endpoint = skill.active ? '/api/skills/deactivate' : '/api/skills/activate';
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectPath, name: skill.name }),
      });
      if (res.ok) fetchSkills();
    } finally {
      setTogglingName(null);
    }
  };

  return (
    <div className="vscode-modal-overlay">
      <div className="vscode-modal flex flex-col" style={{ width: '760px', maxHeight: 'calc(85 * var(--ui-vh))', padding: 0 }}>
        {/* Header */}
        <div className="vscode-modal-header titlebar">
          <div className="vscode-modal-header-title">
            <Store size={16} />
            <span>{t('assetStore.title', 'Asset Store')}</span>
          </div>
          <button className="vscode-modal-close" onClick={onClose} aria-label={t('common.close', 'Close')}>
            <X size={16} />
          </button>
        </div>

        {/* Asset type tabs */}
        <div className="vscode-modal-tabs titlebar">
          <TabButton active={assetType === 'skill'} onClick={() => setAssetType('skill')}>
            {t('assetStore.tabSkills', 'Skills')}
          </TabButton>
          <TabButton active={assetType === 'template'} onClick={() => setAssetType('template')}>
            {t('assetStore.tabTemplates', 'LaTeX templates')}
          </TabButton>
        </div>

        {/* Skills sub-tabs: the catalog and what the project actually runs */}
        {assetType === 'skill' && (
          <div className="vscode-modal-tabs">
            <TabButton active={skillsTab === 'catalog'} onClick={() => setSkillsTab('catalog')}>
              {t('assetStore.tabCatalog', 'Catalog')}
            </TabButton>
            <TabButton active={skillsTab === 'active'} onClick={() => setSkillsTab('active')}>
              {t('assetStore.tabActive', 'Active in project')}
            </TabButton>
          </div>
        )}

        {/* Content */}
        <div className="vscode-modal-content flex-1 overflow-y-auto">
          {!projectPath && (
            <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)', marginBottom: '12px' }}>
              {assetType === 'template'
                ? t('assetStore.noProjectHintTemplates', 'Open a project to install templates.')
                : t('assetStore.noProjectHint', 'Open a project to install or activate skills.')}
            </div>
          )}

          {refreshError && (
            <div style={{ fontSize: '12px', color: 'var(--vscode-error-fg, #f14c4c)', marginBottom: '12px' }}>
              {refreshError}
            </div>
          )}

          {installMessage && (
            <div style={{ fontSize: '12px', color: 'var(--vscode-fg-teal)', marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Check size={13} /> {installMessage}
            </div>
          )}

          {assetType === 'template' && (
            templates.length === 0 ? (
              <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)' }}>
                {t('assetStore.emptyTemplates', 'No templates available in the catalog.')}
              </div>
            ) : (
              <div className="skills-grid">
                {templates.map(template => {
                  const pending = pendingOverwrite && pendingOverwrite.id === template.id;
                  return (
                    <div key={template.id} className="skill-card">
                      <AssetIcon
                        src={template.hasIcon ? `/api/assets/icon?id=${encodeURIComponent(template.id)}` : null}
                        alt={template.name}
                        fallback={FileText}
                      />
                      <div className="skill-card-body">
                        <div className="skill-card-title">{template.name}</div>
                        {template.version && (
                          <div className="skill-card-note">
                            {t('assetStore.version', 'Version {{version}}', { version: template.version })}
                          </div>
                        )}
                        <div className="skill-card-desc">{template.desc}</div>
                        {pending && (
                          <div className="skill-card-note" style={{ color: 'var(--vscode-error-fg, #f14c4c)', fontStyle: 'normal' }}>
                            <AlertTriangle size={11} style={{ verticalAlign: '-1px', marginRight: '4px' }} />
                            {t('assetStore.overwriteWarning', 'These files already exist and will be replaced: {{files}}', {
                              files: pendingOverwrite.conflicts.slice(0, 5).join(', ')
                                + (pendingOverwrite.conflicts.length > 5 ? ` (+${pendingOverwrite.conflicts.length - 5})` : ''),
                            })}
                          </div>
                        )}
                      </div>
                      <button
                        className="vscode-button skill-card-action"
                        disabled={!projectPath || installingId === template.id}
                        onClick={() => handleInstallTemplate(template, pending)}
                        title={t('assetStore.installTemplateHint', 'Unpack this template at the project root.')}
                      >
                        {installingId === template.id ? (
                          t('assetStore.installing', 'Installing…')
                        ) : pending ? (
                          <><AlertTriangle size={13} /> {t('assetStore.confirmOverwrite', 'Replace files')}</>
                        ) : template.installed ? (
                          <><RefreshCw size={13} /> {t('assetStore.reinstall', 'Reinstall')}</>
                        ) : (
                          <><Download size={13} /> {t('assetStore.install', 'Install')}</>
                        )}
                      </button>
                      {pending && (
                        <button
                          className="vscode-button skill-card-action"
                          onClick={() => setPendingOverwrite(null)}
                        >
                          {t('assetStore.cancel', 'Cancel')}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )
          )}

          {assetType === 'skill' && skillsTab === 'catalog' && (
            assets.length === 0 ? (
              <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)' }}>
                {t('assetStore.emptyCatalog', 'No skills available in the catalog.')}
              </div>
            ) : (
              <div className="skills-grid">
                {assets.map(asset => {
                  const installed = installedNames.has(asset.name);
                  return (
                    <div key={asset.id} className="skill-card">
                      <AssetIcon src={asset.hasIcon ? `/api/assets/icon?id=${encodeURIComponent(asset.id)}` : null} alt={asset.name} />
                      <div className="skill-card-body">
                        <div className="skill-card-title">{asset.name}</div>
                        <div className="skill-card-desc">{asset.desc}</div>
                      </div>
                      <button
                        className="vscode-button skill-card-action"
                        disabled={!projectPath || installed || installingId === asset.id}
                        onClick={() => handleInstall(asset)}
                      >
                        {installed ? (
                          <><Check size={13} /> {t('assetStore.installed', 'Installed')}</>
                        ) : installingId === asset.id ? (
                          t('assetStore.installing', 'Installing…')
                        ) : (
                          <><Download size={13} /> {t('assetStore.install', 'Install')}</>
                        )}
                      </button>
                    </div>
                  );
                })}
              </div>
            )
          )}

          {assetType === 'skill' && skillsTab === 'active' && (
            skills.length === 0 ? (
              <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)' }}>
                {t('assetStore.emptyActive', 'No skills found for this project.')}
              </div>
            ) : (
              <div className="skills-grid">
                {skills.map(skill => (
                  <div key={skill.name} className="skill-card">
                    <AssetIcon src={skill.hasIcon ? `/api/skills/icon?name=${encodeURIComponent(skill.name)}&projectPath=${encodeURIComponent(projectPath)}` : null} alt={skill.name} />
                    <div className="skill-card-body">
                      <div className="skill-card-title">{skill.name}</div>
                      <div className="skill-card-desc">{skill.description}</div>
                      {skill.shadowsBundled && (
                        <div className="skill-card-note">
                          {t('assetStore.shadowsBundled', 'A copy in this project is running instead of the bundled skill, so it does not receive updates.')}
                        </div>
                      )}
                    </div>
                    <button
                      className="vscode-button skill-card-action"
                      disabled={skill.mandatory || togglingName === skill.name}
                      onClick={() => handleToggleActive(skill)}
                    >
                      {skill.mandatory ? (
                        t('assetStore.mandatory', 'Mandatory')
                      ) : skill.active ? (
                        t('assetStore.deactivate', 'Deactivate')
                      ) : (
                        <><Plus size={13} /> {t('assetStore.activate', 'Activate')}</>
                      )}
                    </button>
                    {skill.updatable && (
                      <button
                        className="vscode-button skill-card-action"
                        disabled={!skill.outdated || refreshingName === skill.name}
                        onClick={() => handleRefreshLocal(skill, '/api/skills/update')}
                        title={skill.outdated
                          ? t('assetStore.updateLocalHint', 'Replace the local copy with the catalog version.')
                          : t('assetStore.upToDateHint', 'The local copy matches the catalog version.')}
                      >
                        {refreshingName === skill.name ? (
                          t('assetStore.updating', 'Updating…')
                        ) : skill.outdated ? (
                          <><RefreshCw size={13} /> {t('assetStore.updateLocal', 'Update local copy')}</>
                        ) : (
                          <><Check size={13} /> {t('assetStore.upToDate', 'Up to date')}</>
                        )}
                      </button>
                    )}
                    {!skill.updatable && skill.shadowsBundled && (
                      <button
                        className="vscode-button skill-card-action"
                        disabled={refreshingName === skill.name}
                        onClick={() => handleRefreshLocal(skill, '/api/skills/restore-bundled')}
                        title={t('assetStore.restoreBundledHint', 'Delete the local copy and go back to the version that ships with GilzaBots.')}
                      >
                        {refreshingName === skill.name ? (
                          t('assetStore.restoring', 'Restoring…')
                        ) : (
                          <><RotateCcw size={13} /> {t('assetStore.restoreBundled', 'Restore bundled')}</>
                        )}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )
          )}
        </div>

        {/* Footer */}
        <div className="vscode-modal-footer">
          <button onClick={onClose} className="vscode-button">{t('assetStore.close', 'Close')}</button>
        </div>
      </div>
    </div>
  );
}
