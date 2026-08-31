import { describe, expect, it } from 'vitest'

import {
  constraintKinds,
  constraintManifestReady,
  normalizeConstraintManifest,
  suggestConstraintManifest,
} from './constraints.js'

describe('constraint manifest', () => {
  it('recognizes critical buses in the requested board', () => {
    expect(constraintKinds('MCU with I2C sensor and USB-C')).toEqual(['i2c', 'usb'])
  })

  it('does not pretend I2C needs controlled impedance', () => {
    const manifest = suggestConstraintManifest('I2C temperature sensor')
    const bus = manifest.net_classes[0]

    expect(bus.nets).toEqual(['SDA', 'SCL'])
    expect(bus.pullups_required).toBe(true)
    expect(bus.controlled_impedance).toBe(false)
    expect(bus.impedance_ohms).toBeNull()
  })

  it('requires exact nets instead of approving a generic placeholder', () => {
    const generic = suggestConstraintManifest('simple regulator')
    expect(generic.net_classes[0].nets).toEqual([])
    expect(constraintManifestReady(generic)).toBe(false)
    expect(constraintManifestReady(suggestConstraintManifest('I2C sensor'))).toBe(true)
  })

  it('makes the USB impedance and layer-shift budget explicit', () => {
    const manifest = suggestConstraintManifest('USB device')
    const bus = manifest.net_classes[0]

    expect(bus.impedance_ohms).toBe(90)
    expect(bus.max_layer_transitions).toBe(2)
    expect(bus.approved).toBeUndefined()
    expect(manifest.approved).toBe(false)
  })

  it('normalizes editable comma-separated fields into machine data', () => {
    const normalized = normalizeConstraintManifest({
      approved: true,
      board_layers: '4',
      net_classes: [
        {
          name: 'Clock',
          nets: 'CLK, CLK_RET',
          allowed_layers: 'F.Cu, In1.Cu',
          max_layer_transitions: '1',
          max_vias_per_net: '1',
        },
      ],
    })

    expect(normalized.board_layers).toBe(4)
    expect(normalized.net_classes[0].nets).toEqual(['CLK', 'CLK_RET'])
    expect(normalized.net_classes[0].allowed_layers).toEqual(['F.Cu', 'In1.Cu'])
  })
})
