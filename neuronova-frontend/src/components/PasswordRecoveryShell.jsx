import { Link } from 'react-router-dom'
import { ShieldCheck } from 'lucide-react'
import './PasswordRecovery.css'

export default function PasswordRecoveryShell({
  icon: Icon,
  tone = 'default',
  eyebrow = 'Account recovery',
  title,
  description,
  children,
}) {
  return (
    <main className="password-recovery">
      <div className="password-recovery__background" aria-hidden="true" />

      <div className="password-recovery__frame">
        <Link to="/" className="password-recovery__brand" aria-label="NeuroNova home">
          <img
            src="/neuronova-mark.png"
            alt=""
            className="brand-mark brand-mark-on-light password-recovery__brand-mark"
          />
          <span>NeuroNova</span>
        </Link>

        <section className="password-recovery__card" aria-labelledby="password-recovery-title">
          <div className={`password-recovery__icon password-recovery__icon--${tone}`} aria-hidden="true">
            <Icon size={24} strokeWidth={1.9} />
          </div>

          <p className="password-recovery__eyebrow">{eyebrow}</p>
          <h1 id="password-recovery-title" className="password-recovery__title">{title}</h1>
          <p className="password-recovery__description">{description}</p>

          <div className="password-recovery__body">{children}</div>
        </section>

        <p className="password-recovery__security-note">
          <ShieldCheck size={15} aria-hidden="true" />
          Secure password recovery
        </p>
      </div>
    </main>
  )
}
