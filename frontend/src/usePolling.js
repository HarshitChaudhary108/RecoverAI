import { useEffect, useRef } from 'react';

export function usePolling(callback, interval = 30000) {
  const timerRef = useRef(null);

  useEffect(() => {
    // Initial call
    callback();

    timerRef.current = setInterval(() => {
      callback();
    }, interval);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
      }
    };
  }, [callback, interval]);
}
