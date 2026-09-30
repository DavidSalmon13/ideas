import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from './App'
import { getApiToken, setApiToken } from './api/client'
import { mockFetch } from './test/mockFetch'

afterEach(() => setApiToken(''))

describe('App', () => {
  it('persists an entered API token to storage for later requests', async () => {
    mockFetch(() => [])
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    )
    await screen.findByText('No runs yet.') // let Library's initial fetch settle
    const field = screen.getByLabelText('API token')
    await userEvent.type(field, 's3cret')
    expect(getApiToken()).toBe('s3cret')
  })

  it('starts with whatever token was already stored', async () => {
    setApiToken('already-set')
    mockFetch(() => [])
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    )
    await screen.findByText('No runs yet.')
    expect(screen.getByLabelText('API token')).toHaveValue('already-set')
  })
})
