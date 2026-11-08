import React, { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

// The composer's `/` completion list, drawn above the input.
//
// Purely presentational: ChatPanel owns the query, the highlighted row and the
// keyboard (the textarea keeps focus the whole time, so typing narrows the list
// and arrows move the highlight). Rows are chosen on mousedown with the default
// prevented, so a click never blurs the input it completes.
export default function SlashCommandMenu({ commands, loading, error, selectedIndex, onHover, onChoose }) {
  const { t } = useTranslation();
  const listRef = useRef(null);

  useEffect(() => {
    const row = listRef.current?.querySelector(`[data-index="${selectedIndex}"]`);
    row?.scrollIntoView?.({ block: 'nearest' });
  }, [selectedIndex, commands]);

  let status = '';
  if (loading) status = t('slashCommand.loading', 'Loading commands…');
  else if (error) status = t('slashCommand.loadFailed', 'Could not load the commands: {{error}}', { error });
  else if (!commands.length) status = t('slashCommand.noMatch', 'No command matches.');

  return (
    <div className="slash-command-menu" role="listbox" aria-label={t('slashCommand.menuLabel', 'Commands')} ref={listRef}>
      {status ? (
        <div className="slash-command-menu-status">{status}</div>
      ) : commands.map((command, index) => (
        <div
          key={command.name}
          data-index={index}
          role="option"
          aria-selected={index === selectedIndex}
          className={`slash-command-menu-item${index === selectedIndex ? ' is-selected' : ''}`}
          onMouseEnter={() => onHover(index)}
          onMouseDown={(event) => { event.preventDefault(); onChoose(command); }}
        >
          <span className="slash-command-menu-name">{command.name}</span>
          {command.kind === 'skill' && (
            <span className="slash-command-menu-badge">{t('slashCommand.skillBadge', 'skill')}</span>
          )}
          {command.usage && <span className="slash-command-menu-usage">{command.usage}</span>}
          <span className="slash-command-menu-desc">{command.description}</span>
        </div>
      ))}
      <div className="slash-command-menu-hint">
        {t('slashCommand.menuHint', '↑↓ to navigate · Enter to run · Tab to complete · Esc to close')}
      </div>
    </div>
  );
}
