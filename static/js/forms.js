/* Progressive enhancement: native date/time fields still work without JavaScript. */
(() => {
  const dateText = date => [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
  document.querySelectorAll('.editor .form-field').forEach(container => {
    const label = container.querySelector('label');
    container.querySelectorAll('input[type="date"], input[type="time"]').forEach(input => {
      const isDate = input.type === 'date';
      if (label) input.setAttribute('aria-label', label.textContent.replace(/:$/, '') + (isDate ? ' 날짜' : ' 시간'));
      const shortcuts = document.createElement('div');
      shortcuts.className = 'input-shortcuts';
      shortcuts.setAttribute('role', 'group');
      shortcuts.setAttribute('aria-label', input.getAttribute('aria-label') + ' 빠른 선택');
      const add = (text, getValue) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'shortcut';
        button.textContent = text;
        button.addEventListener('click', () => {
          const value = getValue();
          if (!value) return;
          input.value = value;
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.dispatchEvent(new Event('change', {bubbles: true}));
        });
        shortcuts.append(button);
      };
      if (isDate) {
        add('오늘', () => dateText(new Date()));
        add('내일', () => { const day = new Date(); day.setDate(day.getDate() + 1); return dateText(day); });
        const workDate = document.getElementById('id_work_date');
        if (workDate && input !== workDate) add('근무일', () => workDate.value);
      } else {
        const attendance = input.name === 'started_at_1' || input.name === 'ended_at_1';
        const presets = attendance ? ['09:00', '12:00', '18:00'] : ['18:00', '19:00', '20:00', '21:00'];
        presets.forEach(value => add(value, () => value));
      }
      input.insertAdjacentElement('afterend', shortcuts);
    });
  });
})();
