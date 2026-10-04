import { handle } from "../../src/app.js";

export const onRequest = ({ request, env }) => handle(request, env);
