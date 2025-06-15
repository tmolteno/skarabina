# Skarabina

skarabina: a basic 1GC radio astronomy RFI flagger

Intended to reduce I/O costs by performing the standard flagging during 1GC efficiently, and with low memory requirements. Skarabina is named after the latin name Skarabinae, the genus of many interesting beetles in Africa.

## Install

    pip install skarabina
    
## Usage

    
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
