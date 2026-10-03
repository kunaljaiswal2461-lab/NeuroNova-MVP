import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RelationshipDefinitionsPanel from '../src/components/sources/RelationshipDefinitionsPanel'
import { api } from '../src/api/client'

vi.mock('../src/api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const sourceId = 'source-relationships'
const snapshotId = 'snapshot-current'
const selectedAssetDetail = {
  source_id: sourceId,
  snapshot_id: snapshotId,
  scope_matches_current_selection: true,
  asset: {
    asset_id: 'asset-orders',
    schema_name: 'sales',
    table_name: 'orders',
  },
  foreign_keys: [{
    foreign_key_id: 'fk-orders-customer',
    constraint_name: 'orders_customer_id_fkey',
    target_status: 'IN_SCOPE',
    referenced_asset: {
      asset_id: 'asset-customers',
      schema_name: 'sales',
      table_name: 'customers',
    },
    column_mappings: [{
      ordinal_position: 1,
      referencing_column_name: 'customer_id',
      referenced_column_name: 'id',
    }],
  }],
}

const eligibleRelationship = {
  snapshot_id: snapshotId,
  foreign_key_id: 'fk-orders-customer',
  constraint_name: 'orders_customer_id_fkey',
  base_asset_id: 'asset-orders',
  base_schema_name: 'sales',
  base_table_name: 'orders',
  lookup_asset_id: 'asset-customers',
  lookup_schema_name: 'sales',
  lookup_table_name: 'customers',
  column_pairs: [{
    ordinal_position: 1,
    base_column_id: 'column-orders-customer-id',
    base_column_name: 'customer_id',
    lookup_column_id: 'column-customers-id',
    lookup_column_name: 'id',
  }],
  target_key: {
    key_id: 'key-customers-id',
    constraint_name: 'customers_pkey',
    key_kind: 'primary_key',
    lookup_column_ids: ['column-customers-id'],
    lookup_column_names: ['id'],
  },
}

function relationship(versionNumber = 1) {
  return {
    relationship_id: 'relationship-orders-customers',
    version_number: versionNumber,
    snapshot_id: snapshotId,
    foreign_key_id: 'fk-orders-customer',
    base_asset_id: 'asset-orders',
    base_schema_name: 'sales',
    base_table_name: 'orders',
    lookup_asset_id: 'asset-customers',
    lookup_schema_name: 'sales',
    lookup_table_name: 'customers',
    column_pairs: [{ base_column_name: 'customer_id', lookup_column_name: 'id' }],
    join_type: 'left',
    confirmed_by_user_id: 'user-1',
    confirmed_at: '2026-10-03T12:00:00Z',
    state: 'valid',
  }
}

function jwtOptions() {
  return expect.objectContaining({ requireJwt: true, preserveErrorBody: true })
}

function renderPanel() {
  return render(
    <RelationshipDefinitionsPanel
      sourceId={sourceId}
      snapshotId={snapshotId}
      selectedAssetDetail={selectedAssetDetail}
    />,
  )
}

beforeEach(() => {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/relationships/eligible`) {
      return Promise.resolve({ items: [eligibleRelationship], count: 1 })
    }
    if (path === `/sources/${sourceId}/relationships`) return Promise.resolve({ items: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  api.post.mockImplementation(path => {
    if (path === `/sources/${sourceId}/relationships`) return Promise.resolve(relationship())
    throw new Error(`unexpected POST ${path}`)
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it('requires explicit owner confirmation and posts only a declared FK plus controlled join behavior', async () => {
  renderPanel()

  const confirmRelationship = screen.getByRole('button', { name: 'Confirm declared relationship' })
  await waitFor(() => expect(confirmRelationship.disabled).toBe(false))
  fireEvent.click(confirmRelationship)
  const save = await screen.findByRole('button', { name: 'Confirm relationship' })
  expect(save.disabled).toBe(true)
  expect(screen.getByText(/is the base table/i)).toBeTruthy()
  expect(screen.getByText(/customers_pkey/i)).toBeTruthy()
  expect(screen.getAllByText(/Left join — retain unmatched base rows/i).length).toBeGreaterThan(0)

  fireEvent.click(screen.getByRole('checkbox', { name: /I confirm this declared relationship/i }))
  fireEvent.click(save)

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    `/sources/${sourceId}/relationships`,
    {
      owner_confirmed: true,
      snapshot_id: snapshotId,
      foreign_key_id: 'fk-orders-customer',
      join_type: 'left',
    },
    jwtOptions(),
  ))
  expect(await screen.findByText(/relationship was explicitly confirmed/i)).toBeTruthy()
})

it('does not offer catalog FKs that the server did not establish as eligible', async () => {
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/relationships/eligible`) return Promise.resolve({ items: [], count: 0 })
    if (path === `/sources/${sourceId}/relationships`) return Promise.resolve({ items: [], count: 0 })
    throw new Error(`unexpected GET ${path}`)
  })
  render(
    <RelationshipDefinitionsPanel
      sourceId={sourceId}
      snapshotId={snapshotId}
      selectedAssetDetail={{
        ...selectedAssetDetail,
        foreign_keys: [{
          ...selectedAssetDetail.foreign_keys[0],
          target_status: 'TARGET_OUT_OF_SCOPE',
          referenced_asset: null,
        }],
      }}
    />,
  )

  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm declared relationship' }).disabled).toBe(true))
  expect(await screen.findByText(/no eligible declared foreign key/i)).toBeTruthy()
  expect(api.post).not.toHaveBeenCalled()
})

it('does not permit a revision from an unrelated currently selected base table', async () => {
  const flatRelationship = {
    ...relationship(),
    current_catalog: {
      ...eligibleRelationship,
      join_type: 'left',
    },
  }
  api.get.mockImplementation(path => {
    if (path === `/sources/${sourceId}/relationships/eligible`) {
      return Promise.resolve({ items: [eligibleRelationship], count: 1 })
    }
    if (path === `/sources/${sourceId}/relationships`) return Promise.resolve({ items: [flatRelationship], count: 1 })
    if (path === `/sources/${sourceId}/relationships/relationship-orders-customers`) return Promise.resolve(flatRelationship)
    if (path === `/sources/${sourceId}/relationships/relationship-orders-customers/versions`) return Promise.resolve({ items: [flatRelationship], count: 1 })
    throw new Error(`unexpected GET ${path}`)
  })
  render(
    <RelationshipDefinitionsPanel
      sourceId={sourceId}
      snapshotId={snapshotId}
      selectedAssetDetail={{
        ...selectedAssetDetail,
        asset: { asset_id: 'asset-products', schema_name: 'sales', table_name: 'products' },
      }}
    />,
  )

  fireEvent.click(screen.getByRole('button', { name: 'Inspect relationships' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Inspect relationship' }))
  await screen.findByRole('heading', { name: /sales.*orders.*sales.*customers/i })
  expect(screen.getByRole('button', { name: 'Create revised confirmation' }).disabled).toBe(true)
  expect(screen.getByText(/select this relationship’s current base table/i)).toBeTruthy()
})
