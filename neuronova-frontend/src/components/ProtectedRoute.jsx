import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export const ProtectedRoute = ({ children }) => {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[var(--color-base)]">
        <div className="flex flex-col items-center gap-4">
          <div className="w-8 h-8 border-4 border-[var(--color-primary)] border-t-transparent rounded-full animate-spin"></div>
          <p className="text-sm font-medium text-gray-500">Loading your session...</p>
        </div>
      </div>
    );
  }

   if (!user) {
    // Redirect them to the /login page, but save the current location they were
     // trying to go to when they were redirected. This allows us to send them
     // along to that page after they login, which is a nicer user experience
     // than dropping them off on the home page.
     return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // --- TEMPORARY BYPASS FOR UI TESTING ---
  // Commenting out this block prevents the app from kicking you back to /login
  // if (!user) {
  //   // Redirect them to the /login page, but save the current location they were
  //   // trying to go to when they were redirected. This allows us to send them
  //   // along to that page after they login, which is a nicer user experience
  //   // than dropping them off on the home page.
  //   return <Navigate to="/login" state={{ from: location }} replace />;
  // }
  // ---------------------------------------

  return children;
};
