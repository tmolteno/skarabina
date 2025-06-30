# Skarabina

skarabina: a basic 1GC radio astronomy RFI flagger

Author: Tim Molteno (tim@elec.ac.nz)

Intended to reduce I/O costs by performing the standard flagging during 1GC efficiently, and with low memory requirements. Skarabina is named after the latin name Skarabinae, the genus of many interesting beetles (uncluding the dungbeetle) in Africa.

## Install

    pip install skarabina
    
## Usage

### Barber flagging

Generate a report (in the style of barber).

    skarabina --ms foo.ms --barber
    
### Clip and Nan Flagging

This will flag a measurement set, and modify it in-place.

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --apply --clobber
    
The following will write a new measurement set.

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --apply --clobber --msout bar.ms

    
## Build

    pip install poetry
    poetry install

## Stimela

skarabina is available as a stimela package. You can include it in your pipeline (after installing skarabina) thusly

    _include:
        - (skarabina):
            - skarabina.yml
            
    my-recpipe:
        info: "Print a flagging summary using skarabina"
        inputs:
            ms: MS

        steps:
            flag-summary:
                cab: skarabina
                params:
                    ms: =recipe.ms
                    summary: true
