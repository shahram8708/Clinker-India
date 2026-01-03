document.addEventListener('DOMContentLoaded', () => {
  const forms = document.querySelectorAll('form');

  forms.forEach((form) => {
    form.addEventListener('submit', () => {
      const submitters = form.querySelectorAll('button[type="submit"], input[type="submit"]');

      submitters.forEach((control) => {
        if (control.dataset.loadingState === 'on') return;
        control.dataset.loadingState = 'on';
        control.setAttribute('disabled', 'disabled');
        control.setAttribute('aria-busy', 'true');

        if (control.tagName.toLowerCase() === 'button') {
          let label = control.querySelector('.btn-label');
          if (!label) {
            label = document.createElement('span');
            label.className = 'btn-label';
            while (control.firstChild) {
              label.appendChild(control.firstChild);
            }
            control.appendChild(label);
          }

          let spinner = control.querySelector('.btn-spinner');
          if (!spinner) {
            spinner = document.createElement('span');
            spinner.className = 'spinner-border spinner-border-sm me-2 btn-spinner';
            spinner.setAttribute('aria-hidden', 'true');
            control.prepend(spinner);
          }

          if (control.dataset.loadingText) {
            label.textContent = control.dataset.loadingText;
          }

          control.classList.add('loading');
        } else {
          if (!control.dataset.originalValue) {
            control.dataset.originalValue = control.value || '';
          }
          control.value = control.dataset.loadingText || 'Processing...';
          control.classList.add('loading-input');
        }
      });
    });
  });
});
