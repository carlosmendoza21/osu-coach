// Runs before each test file: adds matchers like toBeInTheDocument() to expect(), and
// unmounts what a test rendered so the next test starts from an empty page.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
