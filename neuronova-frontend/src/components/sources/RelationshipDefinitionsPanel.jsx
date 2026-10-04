import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { sourceApi, sourceErrorCode } from '../../api/sources'

const SAFE_ERROR_MESSAGES = {
  authentication_required: 'Your sign-in session is unavailable. Please sign in again to manage approved relationships.',
  source_not_found: 'This saved connection is no longer available.',
  catalog_not_found: 'Publish metadata before confirming a relationship.',
  catalog_snapshot_not_found: 'The displayed metadata snapshot is no longer available. Refresh metadata and try again.',
  catalog_scope_changed: 'The selected table scope changed. Refresh metadata before confirming this relationship.',
  relationship_definition_not_found: 'That approved relationship is no longer available.',
  relationship_definition_conflict: 'This declared relationship is already approved for the source.',
  relationship_definition_needs_review: 'This relationship needs review against current metadata before it can be used.',
  relationship_definition_retired: 'This relationship has been retired.',
  relationship_catalog_unavailable: 'Current authorized metadata is unavailable for this relationship. Refresh metadata and try again.',
  relationship_definition_persistence_failed: 'The relationship could not be saved. Your confirmation was not assumed.',
  relationship_snapshot_changed: 'Metadata changed while this relationship was being prepared. Review the current table and try again.',
  invalid_relationship_definition: 'The selected foreign key is not an eligible same-source lookup relationship.',
}

function safeErrorMessage(error, fallback = 'We could not complete that relationship request. Please try again.') {
  return SAFE_ERROR_MESSAGES[sourceErrorCode(error)] || fallback
}

function quoteIdentifier(identifier) {
  return `"${String(identifier || '').replace(/"/g, '""')}"`
}

function assetLabel(asset) {
  if (!asset) return 'Unknown table'
  return `${quoteIdentifier(asset.schema_name)}.${quoteIdentifier(asset.table_name)}`
}

function fkId(foreignKey) {
  return foreignKey?.foreign_key_id ?? foreignKey?.id ?? null
}

function assetId(asset) {
  return asset?.asset_id ?? asset?.id ?? null
}

function eligibleForeignKey(item) {
  if (!item?.foreign_key_id || !item?.base_asset_id || !item?.lookup_asset_id) return null
  return {
    ...item,
    referenced_asset: {
      asset_id: item.lookup_asset_id,
      schema_name: item.lookup_schema_name,
      table_name: item.lookup_table_name,
    },
    column_mappings: (Array.isArray(item.column_pairs) ? item.column_pairs : []).map(pair => ({
      ordinal_position: pair.ordinal_position,
      referencing_column_name: pair.base_column_name,
      referenced_column_name: pair.lookup_column_name,
    })),
  }
}

function directionLabel(joinType) {
  return String(joinType || '').toLowerCase() === 'inner'
    ? 'Inner join — only base rows with a lookup match'
    : 'Left join — retain unmatched base rows'
}

function stateLabel(state) {
  const normalized = String(state || '').toLowerCase()
  if (normalized === 'valid' || normalized === 'active') return 'Active'
  if (normalized === 'needs_review') return 'Needs review'
  if (normalized === 'retired') return 'Retired'
  return 'Status unavailable'
}

function stateClass(state) {
  const normalized = String(state || '').toLowerCase()
  if (normalized === 'valid' || normalized === 'active') return 'relationship-state-valid'
  if (normalized === 'needs_review') return 'relationship-state-review'
  if (normalized === 'retired') return 'relationship-state-retired'
  return 'relationship-state-unknown'
}

function dateText(value) {
  if (!value) return 'Confirmation time unavailable'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Confirmation time unavailable' : parsed.toLocaleString()
}

function relationItems(response) {
  const items = response?.items ?? response?.relationships ?? response
  return Array.isArray(items) ? items : []
}

function relationFromResponse(response) {
  return response?.relationship ?? response?.item ?? response
}

function relationshipId(relation) {
  return relation?.relationship_id ?? relation?.id ?? null
}

function currentVersion(relation, versions = []) {
  if (relation?.current_version) return relation.current_version
  if (relation?.version_number != null) return relation
  return [...versions].sort((left, right) => (right.version_number ?? 0) - (left.version_number ?? 0))[0] ?? null
}

function baseAsset(relation) {
  const nested = relation?.base_asset ?? relation?.base ?? relation?.current_catalog?.base_asset
  if (nested) return nested
  const current = relation?.current_catalog ?? relation
  if (current?.base_asset_id && current?.base_schema_name && current?.base_table_name) {
    return {
      asset_id: current.base_asset_id,
      schema_name: current.base_schema_name,
      table_name: current.base_table_name,
    }
  }
  return null
}

function lookupAsset(relation) {
  const nested = relation?.lookup_asset ?? relation?.lookup ?? relation?.current_catalog?.lookup_asset
  if (nested) return nested
  const current = relation?.current_catalog ?? relation
  if (current?.lookup_asset_id && current?.lookup_schema_name && current?.lookup_table_name) {
    return {
      asset_id: current.lookup_asset_id,
      schema_name: current.lookup_schema_name,
      table_name: current.lookup_table_name,
    }
  }
  return null
}

function mappings(relation) {
  const value = relation?.column_pairs ?? relation?.column_mappings ?? relation?.mappings ?? []
  return Array.isArray(value) ? value : []
}

function relationshipTitle(relationship) {
  if (relationship?.name || relationship?.display_name) return relationship.name || relationship.display_name
  const base = baseAsset(relationship)
  const lookup = lookupAsset(relationship)
  return base && lookup ? `${assetLabel(base)} → ${assetLabel(lookup)}` : 'Approved relationship'
}

function mappingLabel(mapping) {
  const base = mapping?.base_column_name ?? mapping?.referencing_column_name ?? mapping?.source_column_name ?? 'base column'
  const lookup = mapping?.lookup_column_name ?? mapping?.referenced_column_name ?? mapping?.target_column_name ?? 'lookup column'
  return `${quoteIdentifier(base)} → ${quoteIdentifier(lookup)}`
}

function targetKeyLabel(foreignKey) {
  const key = foreignKey?.target_key
  if (!key) return 'Verified current primary or unique key'
  const kind = String(key.key_kind || '').toLowerCase() === 'primary_key' ? 'Primary key' : 'Unique key'
  const columns = Array.isArray(key.lookup_column_names)
    ? key.lookup_column_names.map(quoteIdentifier).join(', ')
    : ''
  return `${kind}${key.constraint_name ? ` ${quoteIdentifier(key.constraint_name)}` : ''}${columns ? ` · ${columns}` : ''}`
}

function RelationshipCard({ relationship, onInspect, loading }) {
  const version = currentVersion(relationship)
  return (
    <article className="relationship-definition-card">
      <div className="relationship-definition-card-heading">
        <div>
          <h5>{relationshipTitle(relationship)}</h5>
          <p>{version?.version_number ? `Version ${version.version_number}` : 'Version unavailable'}</p>
        </div>
        <span className={`relationship-state-chip ${stateClass(relationship?.state)}`}>{stateLabel(relationship?.state)}</span>
      </div>
      <p className="relationship-definition-direction">
        {assetLabel(baseAsset(relationship))} <span aria-hidden="true">→</span> {assetLabel(lookupAsset(relationship))}
      </p>
      <p className="relationship-definition-semantics">{directionLabel(version?.join_type ?? relationship?.join_type)}</p>
      <div className="source-actions">
        <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => onInspect(relationshipId(relationship))} disabled={loading || !relationshipId(relationship)}>
          {loading ? 'Loading…' : 'Inspect relationship'}
        </button>
      </div>
    </article>
  )
}

export default function RelationshipDefinitionsPanel({ sourceId, snapshotId, selectedAssetDetail }) {
  const [open, setOpen] = useState(false)
  const [relationships, setRelationships] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [selectedRelationship, setSelectedRelationship] = useState(null)
  const [versions, setVersions] = useState([])
  const [inspecting, setInspecting] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingRelationship, setEditingRelationship] = useState(null)
  const [foreignKeyId, setForeignKeyId] = useState('')
  const [joinType, setJoinType] = useState('left')
  const [confirmed, setConfirmed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [retiring, setRetiring] = useState(false)
  const [eligibleItems, setEligibleItems] = useState([])
  const [eligibleLoading, setEligibleLoading] = useState(false)

  const mountedRef = useRef(false)
  const listControllerRef = useRef(null)
  const detailControllerRef = useRef(null)
  const mutationControllerRef = useRef(null)
  const eligibleControllerRef = useRef(null)
  const listRequestRef = useRef(0)
  const detailRequestRef = useRef(0)
  const eligibleRequestRef = useRef(0)

  const selectedAsset = selectedAssetDetail?.asset ?? null
  const selectedAssetId = assetId(selectedAsset)
  const eligibleForeignKeys = useMemo(() => {
    if (!snapshotId || !selectedAssetId) return []
    return eligibleItems
      .filter(item => (
        String(item?.snapshot_id) === String(snapshotId)
        && String(item?.base_asset_id) === String(selectedAssetId)
      ))
      .map(eligibleForeignKey)
      .filter(Boolean)
  }, [eligibleItems, selectedAssetId, snapshotId])
  const currentForeignKey = eligibleForeignKeys.find(item => String(fkId(item)) === String(foreignKeyId)) ?? null
  const canConfirm = Boolean(snapshotId && selectedAssetId && currentForeignKey && !eligibleLoading)

  const loadRelationships = useCallback(async () => {
    listControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = listRequestRef.current + 1
    listRequestRef.current = requestId
    listControllerRef.current = controller
    setLoading(true)
    try {
      const response = await sourceApi.listRelationshipDefinitions(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestId !== listRequestRef.current) return []
      const items = relationItems(response)
      setRelationships(items)
      setError(null)
      return items
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) {
        setError(safeErrorMessage(requestError, 'We could not load approved relationships.'))
      }
      return []
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === listRequestRef.current) setLoading(false)
    }
  }, [sourceId])

  const loadEligibleRelationships = useCallback(async () => {
    eligibleControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = eligibleRequestRef.current + 1
    eligibleRequestRef.current = requestId
    eligibleControllerRef.current = controller
    if (!snapshotId) {
      setEligibleItems([])
      setEligibleLoading(false)
      return []
    }
    setEligibleLoading(true)
    try {
      const response = await sourceApi.listEligibleDeclaredRelationships(sourceId, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || requestId !== eligibleRequestRef.current) return []
      const items = relationItems(response).filter(item => String(item?.snapshot_id) === String(snapshotId))
      setEligibleItems(items)
      return items
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current && requestId === eligibleRequestRef.current) {
        setEligibleItems([])
        setError(safeErrorMessage(requestError, 'We could not verify eligible declared relationships.'))
      }
      return []
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === eligibleRequestRef.current) setEligibleLoading(false)
    }
  }, [snapshotId, sourceId])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      listRequestRef.current += 1
      detailRequestRef.current += 1
      eligibleRequestRef.current += 1
      listControllerRef.current?.abort()
      detailControllerRef.current?.abort()
      mutationControllerRef.current?.abort()
      eligibleControllerRef.current?.abort()
    }
  }, [])

  useEffect(() => {
    // Eligibility is meaningful only for the base table currently selected
    // in the catalog. Avoid an unrelated owner API call (and an extra alert)
    // while the catalog browser has not selected one.
    if (!selectedAssetId || !snapshotId) return undefined
    // Start after the subscription/cleanup boundary exists. This avoids a
    // synchronous state update in the effect and prevents a StrictMode
    // setup/cleanup cycle from committing an obsolete eligibility response.
    const timer = window.setTimeout(() => { void loadEligibleRelationships() }, 0)
    return () => window.clearTimeout(timer)
  }, [loadEligibleRelationships, selectedAssetId, snapshotId])

  const inspectRelationship = useCallback(async id => {
    if (!id || inspecting) return
    detailControllerRef.current?.abort()
    const controller = new AbortController()
    const requestId = detailRequestRef.current + 1
    detailRequestRef.current = requestId
    detailControllerRef.current = controller
    setInspecting(true)
    setError(null)
    try {
      const [relationshipResponse, versionsResponse] = await Promise.all([
        sourceApi.getRelationshipDefinition(sourceId, id, { signal: controller.signal }),
        sourceApi.listRelationshipDefinitionVersions(sourceId, id, { signal: controller.signal }),
      ])
      if (controller.signal.aborted || !mountedRef.current || requestId !== detailRequestRef.current) return
      setSelectedRelationship(relationFromResponse(relationshipResponse))
      setVersions(relationItems(versionsResponse))
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current && requestId === detailRequestRef.current) {
        setError(safeErrorMessage(requestError, 'We could not load that approved relationship.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current && requestId === detailRequestRef.current) setInspecting(false)
    }
  }, [inspecting, sourceId])

  const openEditor = ({ revision = false } = {}) => {
    if (eligibleLoading) {
      setError('Eligible declared relationships are still being checked. Please wait a moment.')
      return
    }
    if (!snapshotId || !selectedAssetId || eligibleForeignKeys.length === 0) {
      setError('Select a current table with an eligible declared foreign key before confirming a relationship.')
      return
    }
    const selected = revision ? selectedRelationship : null
    const version = currentVersion(selected, versions)
    let suggestedFk = eligibleForeignKeys[0]
    if (revision) {
      const relationshipBaseAssetId = assetId(baseAsset(selected))
      const currentCatalogForeignKeyId = selected?.current_catalog?.foreign_key_id
      if (!selected || !relationshipBaseAssetId || String(relationshipBaseAssetId) !== String(selectedAssetId)) {
        setError('Select this relationship’s current base table before creating a revised confirmation.')
        return
      }
      suggestedFk = eligibleForeignKeys.find(item => (
        String(fkId(item)) === String(currentCatalogForeignKeyId)
      ))
      if (!suggestedFk) {
        setError('This relationship has no matching eligible current catalog foreign key. Refresh metadata and review it before creating a revised confirmation.')
        return
      }
    }
    setForeignKeyId(String(fkId(suggestedFk)))
    setJoinType(String(version?.join_type ?? selected?.join_type ?? 'left').toLowerCase())
    setConfirmed(false)
    setEditingRelationship(selected)
    setEditorOpen(true)
    setError(null)
    setNotice(revision
      ? 'Saving creates a fresh immutable owner confirmation. The previous relationship version remains in history.'
      : 'A relationship is available to database chat only after you explicitly confirm this declared foreign key and join behavior.')
  }

  const closeEditor = () => {
    setEditorOpen(false)
    setEditingRelationship(null)
    setForeignKeyId('')
    setJoinType('left')
    setConfirmed(false)
  }

  const saveRelationship = async event => {
    event.preventDefault()
    if (saving) return
    if (!confirmed) {
      setError('Review the direction and join behavior, then explicitly confirm before saving.')
      return
    }
    if (!canConfirm) {
      setError('Choose an in-scope declared foreign key from the current displayed table.')
      return
    }
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setSaving(true)
    setError(null)
    const payload = {
      owner_confirmed: true,
      snapshot_id: snapshotId,
      foreign_key_id: fkId(currentForeignKey),
      join_type: joinType,
    }
    try {
      const relationshipIdForRevision = relationshipId(editingRelationship)
      const response = relationshipIdForRevision
        ? await sourceApi.createRelationshipDefinitionVersion(sourceId, relationshipIdForRevision, payload, { signal: controller.signal })
        : await sourceApi.createRelationshipDefinition(sourceId, payload, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      const saved = relationFromResponse(response)
      const items = await loadRelationships()
      const refreshed = items.find(item => String(relationshipId(item)) === String(relationshipId(saved))) ?? saved
      setSelectedRelationship(refreshed)
      setVersions([])
      closeEditor()
      setNotice('The relationship was explicitly confirmed. Its approved direction and join behavior will be revalidated before it can run.')
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(safeErrorMessage(requestError, 'We could not save this relationship confirmation.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setSaving(false)
    }
  }

  const retireRelationship = async () => {
    const id = relationshipId(selectedRelationship)
    if (!id || retiring) return
    mutationControllerRef.current?.abort()
    const controller = new AbortController()
    mutationControllerRef.current = controller
    setRetiring(true)
    setError(null)
    try {
      const response = await sourceApi.retireRelationshipDefinition(sourceId, id, { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current) return
      const retired = relationFromResponse(response)
      setSelectedRelationship(retired)
      const items = await loadRelationships()
      setRelationships(items)
      setNotice('The relationship is retired. Historical execution provenance remains intact, but new joins cannot use it.')
    } catch (requestError) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(safeErrorMessage(requestError, 'We could not retire that relationship.'))
      }
    } finally {
      if (!controller.signal.aborted && mountedRef.current) setRetiring(false)
    }
  }

  const selectedVersion = currentVersion(selectedRelationship, versions)
  const selectedState = String(selectedRelationship?.state || '').toLowerCase()
  const selectedRelationshipBaseAssetId = assetId(baseAsset(selectedRelationship))
  const selectedRelationshipCurrentForeignKeyId = selectedRelationship?.current_catalog?.foreign_key_id
  const canReviseSelectedRelationship = Boolean(
    selectedRelationship
    && selectedState !== 'retired'
    && !eligibleLoading
    && selectedRelationshipBaseAssetId
    && String(selectedRelationshipBaseAssetId) === String(selectedAssetId)
    && eligibleForeignKeys.some(item => (
      String(fkId(item)) === String(selectedRelationshipCurrentForeignKeyId)
    )),
  )

  return (
    <section className="relationship-definitions" aria-labelledby="relationship-definitions-heading">
      <div className="relationship-definitions-heading">
        <div>
          <h4 id="relationship-definitions-heading">Approved relationships</h4>
          <p>Only an owner-confirmed declared foreign key from the current source can be used for a controlled base-to-lookup join.</p>
        </div>
        <div className="relationship-definitions-actions source-actions">
          <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => { setOpen(true); void loadRelationships() }} disabled={loading}>
            {open ? (loading ? 'Refreshing…' : 'Refresh') : 'Inspect relationships'}
          </button>
          <button className="btn btn-primary btn-sm" type="button" onClick={() => openEditor()} disabled={!snapshotId || !selectedAssetId || eligibleLoading || eligibleForeignKeys.length === 0}>
            Confirm declared relationship
          </button>
        </div>
      </div>

      {!selectedAssetId && <p className="relationship-definition-guidance">Select a base table above to inspect its in-scope declared foreign keys.</p>}
      {selectedAssetId && eligibleLoading && <p className="relationship-definition-guidance" role="status">Checking current eligible declared relationships…</p>}
      {selectedAssetId && !eligibleLoading && eligibleForeignKeys.length === 0 && <p className="relationship-definition-guidance">This table has no eligible declared foreign key for the controlled lookup join.</p>}
      {error && <p className="source-alert" role="alert">{error}</p>}
      {notice && <p className="source-notice" role="status">{notice}</p>}

      {open && (
        <>
          {loading && relationships.length === 0 ? <p className="source-empty" role="status">Loading approved relationships…</p> : null}
          {!loading && relationships.length === 0 ? <p className="relationship-definition-guidance">No relationship has been confirmed for this source yet.</p> : null}
          {relationships.length > 0 && (
            <div className="relationship-definition-list" aria-label="Approved relationships">
              {relationships.map(relationship => (
                <RelationshipCard
                  key={relationshipId(relationship) ?? `${relationship?.name}-${relationship?.version_number}`}
                  relationship={relationship}
                  onInspect={inspectRelationship}
                  loading={inspecting}
                />
              ))}
            </div>
          )}
        </>
      )}

      {selectedRelationship && !editorOpen && (
        <article className="relationship-definition-detail" aria-labelledby="relationship-definition-detail-heading">
          <div className="relationship-definition-card-heading">
            <div>
              <h5 id="relationship-definition-detail-heading">{relationshipTitle(selectedRelationship)}</h5>
              <p>{stateLabel(selectedRelationship.state)} · confirmed {dateText(selectedVersion?.confirmed_at ?? selectedRelationship.confirmed_at)}</p>
            </div>
            <span className={`relationship-state-chip ${stateClass(selectedRelationship.state)}`}>{stateLabel(selectedRelationship.state)}</span>
          </div>
          <p className="relationship-definition-direction">
            <strong>Base:</strong> {assetLabel(baseAsset(selectedRelationship))} <span aria-hidden="true">→</span> <strong>Lookup:</strong> {assetLabel(lookupAsset(selectedRelationship))}
          </p>
          <p className="relationship-definition-semantics">{directionLabel(selectedVersion?.join_type ?? selectedRelationship.join_type)}. This direction must not multiply base rows; lookup-key evidence is rechecked before execution.</p>
          {mappings(selectedVersion ?? selectedRelationship).length > 0 && (
            <ul className="relationship-definition-mappings" aria-label="Ordered key mappings">
              {mappings(selectedVersion ?? selectedRelationship).map((mapping, index) => <li key={`${mappingLabel(mapping)}-${index}`}>{mappingLabel(mapping)}</li>)}
            </ul>
          )}
          <div className="source-actions">
            <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={() => openEditor({ revision: true })} disabled={!canReviseSelectedRelationship}>Create revised confirmation</button>
            <button className="btn btn-danger btn-sm" type="button" onClick={retireRelationship} disabled={retiring || selectedState === 'retired'}>{retiring ? 'Retiring…' : selectedState === 'retired' ? 'Retired' : 'Retire relationship'}</button>
          </div>
          {!canReviseSelectedRelationship && selectedState !== 'retired' && (
            <p className="relationship-definition-guidance">
              Select this relationship’s current base table and an eligible current catalog foreign key before creating a revised confirmation.
            </p>
          )}
        </article>
      )}

      {editorOpen && (
        <form className="relationship-definition-editor" onSubmit={saveRelationship}>
          <div className="relationship-definition-card-heading">
            <div>
              <h5>{editingRelationship ? 'Confirm a revised relationship version' : 'Confirm a declared relationship'}</h5>
              <p>The mapping is read directly from the selected current catalog foreign key. It cannot be typed or inferred here.</p>
            </div>
            <button className="btn btn-ghost-neutral btn-sm" type="button" onClick={closeEditor} disabled={saving}>Close</button>
          </div>
          <div className="source-form-grid relationship-definition-form-grid">
            <label className="source-field">
              <span>Declared foreign key</span>
              <select value={foreignKeyId} onChange={event => { setForeignKeyId(event.target.value); setConfirmed(false) }} disabled={saving}>
                {eligibleForeignKeys.map(foreignKey => (
                  <option key={fkId(foreignKey)} value={fkId(foreignKey)}>
                    {foreignKey.constraint_name || 'Unnamed foreign key'} · {assetLabel(selectedAsset)} → {assetLabel(foreignKey.referenced_asset)}
                  </option>
                ))}
              </select>
            </label>
            <label className="source-field">
              <span>Join behavior</span>
              <select value={joinType} onChange={event => { setJoinType(event.target.value); setConfirmed(false) }} disabled={saving}>
                <option value="left">Left join — retain unmatched base rows</option>
                <option value="inner">Inner join — require a lookup match</option>
              </select>
            </label>
          </div>
          {currentForeignKey && (
            <section className="relationship-definition-preview" aria-label="Relationship confirmation preview">
              <h6>Plain-language confirmation</h6>
              <p>{assetLabel(selectedAsset)} is the base table. {assetLabel(currentForeignKey.referenced_asset)} is the uniquely keyed lookup table. The ordered mapping is {(currentForeignKey.column_mappings || []).map(mappingLabel).join(', ') || 'read from the declared foreign key'}. {directionLabel(joinType)}.</p>
              <p><strong>Lookup key evidence:</strong> {targetKeyLabel(currentForeignKey)}. It is rechecked before execution so this direction cannot multiply base rows.</p>
            </section>
          )}
          <label className="relationship-confirmation">
            <input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={saving} />
            <span>I confirm this declared relationship and its join behavior for this source. I understand it can be retired and will be revalidated before execution.</span>
          </label>
          <div className="source-actions">
            <button className="btn btn-primary" type="submit" disabled={saving || !confirmed || !canConfirm}>{saving ? 'Saving confirmation…' : editingRelationship ? 'Save new relationship version' : 'Confirm relationship'}</button>
            <button className="btn btn-ghost-neutral" type="button" onClick={closeEditor} disabled={saving}>Cancel</button>
          </div>
        </form>
      )}
    </section>
  )
}
