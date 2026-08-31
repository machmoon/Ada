import { describe, expect, it } from 'vitest'

import { normalizeRequest } from './api.js'
import { DATASHEET_PRESETS } from './datasheets.js'

describe('datasheet presets', () => {
  it('includes the public AMS1117-3.3 PDF', () => {
    expect(DATASHEET_PRESETS).toContainEqual({
      part: 'AMS1117-3.3',
      manufacturer: 'Advanced Monolithic Systems',
      url: 'https://datasheet.lcsc.com/lcsc/1811142212_Advanced-Monolithic-Systems-AMS1117-3-3_C6186.pdf',
    })
  })

  it('can be copied into a ready-to-submit request', () => {
    const preset = DATASHEET_PRESETS[0]
    const request = normalizeRequest({
      intent: 'a 3.3 V LDO regulator board',
      datasheets: { [preset.part]: preset.url },
      time_limit_s: 20,
      review: true,
    })

    expect(request.datasheets).toEqual({ [preset.part]: preset.url })
  })
})
