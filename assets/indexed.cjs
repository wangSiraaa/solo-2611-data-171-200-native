function run(){ trigger(); }
      function trigger(){throw new Error("indexed failure")}
run();
