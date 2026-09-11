# Company logos for table images

Drop a PNG here named by the company `key` from `config.yaml` (`companies.feeds` or
`branding.companies`), e.g. `amgen.png`, `regeneron.png`. When a fact-checked table has
a company column (`branding.company_columns`), `draft/branding.py` draws that logo in the
cell next to the name and appends the ticker from the same config entry.

Nothing is downloaded: a company without a file here gets no logo, a company without a
`ticker:` gets no ticker. Keep logos small (about 400 px wide, transparent background);
they are scaled to the row height. Logos are trademarks of their owners; use the
official press-kit files.
