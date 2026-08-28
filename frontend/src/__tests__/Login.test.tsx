import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import Login from '../Login'

const mockOnLogin = vi.fn()
const mockOnGuest = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Login', () => {
  it('renders username and password fields', () => {
    render(<Login api="http://test" onLogin={mockOnLogin} />)
    expect(screen.getByPlaceholderText('Username')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Password')).toBeInTheDocument()
  })

  it('disables submit when fields are empty', () => {
    render(<Login api="http://test" onLogin={mockOnLogin} />)
    expect(screen.getByRole('button', { name: /sign in/i })).toBeDisabled()
  })

  it('shows error on failed login', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'Invalid credentials' }),
    }) as unknown as typeof fetch
    render(<Login api="http://test" onLogin={mockOnLogin} />)
    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'user' } })
    fireEvent.change(screen.getByPlaceholderText('Password'), { target: { value: 'pass' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(screen.getByText('Invalid credentials')).toBeInTheDocument())
  })

  it('calls onLogin on successful login', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        access_token: 'tok',
        refresh_token: 'rtok',
        user: { id: 1, username: 'admin', role: 'admin', permissions: [] },
      }),
    }) as unknown as typeof fetch
    render(<Login api="http://test" onLogin={mockOnLogin} />)
    fireEvent.change(screen.getByPlaceholderText('Username'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByPlaceholderText('Password'), { target: { value: 'pass' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(mockOnLogin).toHaveBeenCalledOnce())
  })

  it('does not show guest button without onGuest prop', () => {
    render(<Login api="http://test" onLogin={mockOnLogin} />)
    expect(screen.queryByText('Continue as guest')).not.toBeInTheDocument()
  })

  it('shows guest button when onGuest prop provided', () => {
    render(<Login api="http://test" onLogin={mockOnLogin} onGuest={mockOnGuest} />)
    expect(screen.getByText('Continue as guest')).toBeInTheDocument()
  })

  it('calls onGuest when guest button clicked', () => {
    render(<Login api="http://test" onLogin={mockOnLogin} onGuest={mockOnGuest} />)
    fireEvent.click(screen.getByText('Continue as guest'))
    expect(mockOnGuest).toHaveBeenCalledOnce()
  })
})
