// Cognito sign-up / sign-in using the Secure Remote Password (SRP) protocol:
// the password itself is never sent over the network.
import {
  AuthenticationDetails, CognitoUser, CognitoUserAttribute, CognitoUserPool,
} from 'amazon-cognito-identity-js';

// Local dev only: when set, this app is pointed at the local moto Cognito
// mock instead of real AWS. That mock generates a brand new user pool and
// client id every time it restarts, so instead of baking a possibly-stale
// id in at build time, fetch the CURRENT one from the backend at runtime.
const LOCAL_ENDPOINT = import.meta.env.VITE_COGNITO_ENDPOINT;
const API = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '');

let poolPromise = null;
function getPool() {
  if (!poolPromise) {
    poolPromise = (async () => {
      if (!LOCAL_ENDPOINT) {
        return new CognitoUserPool({
          UserPoolId: import.meta.env.VITE_USER_POOL_ID,
          ClientId: import.meta.env.VITE_USER_POOL_CLIENT_ID,
        });
      }
      const res = await fetch(`${API}/config`);
      if (!res.ok) throw new Error('Could not reach the local backend to fetch Cognito config.');
      const cfg = await res.json();
      return new CognitoUserPool({
        UserPoolId: cfg.userPoolId,
        ClientId: cfg.userPoolClientId,
        endpoint: cfg.cognitoEndpoint,
      });
    })();
  }
  return poolPromise;
}

const userFor = async (email) => new CognitoUser({ Username: email, Pool: await getPool() });

export const signUp = async (email, password) => new Promise((resolve, reject) =>
  getPool().then((pool) => pool.signUp(email, password, [new CognitoUserAttribute({ Name: 'email', Value: email })], null,
    (err, res) => (err ? reject(err) : resolve(res)))));

export const confirmSignUp = async (email, code) => new Promise((resolve, reject) =>
  userFor(email).then((u) => u.confirmRegistration(code, true, (err, res) => (err ? reject(err) : resolve(res)))));

export const resendCode = async (email) => new Promise((resolve, reject) =>
  userFor(email).then((u) => u.resendConfirmationCode((err, res) => (err ? reject(err) : resolve(res)))));

export const signIn = async (email, password) => new Promise((resolve, reject) =>
  userFor(email).then((u) => u.authenticateUser(new AuthenticationDetails({ Username: email, Password: password }), {
    onSuccess: resolve,
    onFailure: reject,
  })));

// Returns the current ID token (refreshing it automatically if expired) or null.
export const getIdToken = async () => {
  const pool = await getPool();
  const user = pool.getCurrentUser();
  if (!user) return null;
  return new Promise((resolve) => {
    user.getSession((err, session) => resolve(err || !session?.isValid() ? null : session.getIdToken().getJwtToken()));
  });
};

export const currentEmail = async () => {
  const pool = await getPool();
  const user = pool.getCurrentUser();
  if (!user) return null;
  return new Promise((resolve) => {
    user.getSession((err, session) => resolve(err || !session ? null : session.getIdToken().payload.email));
  });
};

export const signOut = async () => (await getPool()).getCurrentUser()?.signOut();
